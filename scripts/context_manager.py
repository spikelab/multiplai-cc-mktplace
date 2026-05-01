"""Context manager hook for multiplai plugin.

Manages context assembly for user prompts through memory files.
Uses path resolver for all file locations. Memory files are ranked by
metadata (recency + size) and only top candidates are read, staying
within the 5-second hook timeout (R2 mitigation).

Catalog-first read paths (Design Decision 8): when a valid catalog
exists for a context type, entries are read from the catalog without
scanning raw source files. Missing or corrupt catalogs fall back to
live scanning (fail-open). Warnings are emitted once per session per
catalog to avoid log spam.
"""

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.venv_guard import ensure_venv_python
ensure_venv_python()

from lib.paths import get_paths
from lib.log_utils import setup_logging
from lib.model_client import create_client  # D3: LLM calls via ModelClient abstraction
from lib.memory_router import create_router
from generators.base import CATALOG_SCHEMA_VERSION

logger = setup_logging("context_manager")

# Catalog cache staleness threshold in seconds (15 minutes)
_CATALOG_CACHE_TTL = 900
_CATALOG_FILENAME = "memory_catalog.json"

# Catalog types supported by _read_catalog_or_scan()
_KNOWN_CATALOG_TYPES = frozenset({"memory", "diary", "skills", "resources"})

# Once-per-session warning deduplication (Decision 8).
# Keyed by full catalog file path to correctly dedup across
# different data directories.
_catalog_warnings_emitted: set[str] = set()


@dataclass
class RankedFile:
    """A memory file ranked by metadata."""
    path: Path
    size: int
    mtime: float
    score: float


def _iter_markdown_files(directory: Path):
    """Yield ``.md`` files in *directory*, skipping non-files and subdirs."""
    if not directory.exists():
        return
    for f in directory.iterdir():
        if f.is_file() and f.suffix == ".md":
            yield f


# Scoring weights for metadata-first ranking (R2 mitigation)
_RECENCY_WEIGHT = 0.7
_SIZE_WEIGHT = 0.3
_RECENCY_DECAY_DAYS = 60
_SIZE_NORM_BYTES = 10_000


def _rank_memory_files(memory_dir: Path) -> list[RankedFile]:
    """Rank memory files by metadata (mtime, size) without reading content.

    More recently modified files and larger files score higher, ensuring
    the most relevant context is prioritised under the 5-second timeout (R2).
    """
    ranked: list[RankedFile] = []
    now = time.time()

    for f in _iter_markdown_files(memory_dir):
        try:
            st = f.stat()
        except OSError:
            continue
        age = now - st.st_mtime
        recency_score = max(0.0, 1.0 - age / (86400 * _RECENCY_DECAY_DAYS))
        size_score = min(1.0, st.st_size / _SIZE_NORM_BYTES)
        score = recency_score * _RECENCY_WEIGHT + size_score * _SIZE_WEIGHT
        ranked.append(RankedFile(path=f, size=st.st_size, mtime=st.st_mtime, score=score))

    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked


def _read_memory_files(memory_dir: Path) -> dict[str, str]:
    """Read all markdown memory files from *memory_dir*.

    Returns a dict mapping filename to content.  Missing or unreadable
    files are silently skipped.
    """
    result: dict[str, str] = {}
    for f in _iter_markdown_files(memory_dir):
        try:
            result[f.name] = f.read_text()
        except Exception:
            logger.warning("Failed to read memory file: %s", f)
    return result


def _read_top_memory_files(memory_dir: Path, *, max_files: int = 10) -> dict[str, str]:
    """Rank memory files by metadata, then read only the top candidates.

    This is the key R2 mitigation: avoid reading all files to stay under
    the 5-second timeout on large memory sets.
    """
    ranked = _rank_memory_files(memory_dir)
    result: dict[str, str] = {}
    for item in ranked[:max_files]:
        try:
            result[item.path.name] = item.path.read_text()
        except Exception:
            logger.warning("Failed to read ranked memory file: %s", item.path)
    return result


# ---------------------------------------------------------------------------
# Catalog caching in $data_dir/catalogs/
# ---------------------------------------------------------------------------

def _cache_catalog(catalogs_dir: Path, catalog_data: dict) -> None:
    """Write catalog data to the cache directory."""
    catalogs_dir.mkdir(parents=True, exist_ok=True)
    cache_file = catalogs_dir / _CATALOG_FILENAME
    cache_file.write_text(json.dumps(catalog_data, indent=2))


def _load_cached_catalog(catalogs_dir: Path) -> dict | None:
    """Load a cached catalog from disk, or return None if absent."""
    cache_file = catalogs_dir / _CATALOG_FILENAME
    if not cache_file.exists():
        return None
    try:
        return json.loads(cache_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _is_catalog_fresh(catalogs_dir: Path) -> bool:
    """Check whether the cached catalog is still within the TTL."""
    cache_file = catalogs_dir / _CATALOG_FILENAME
    if not cache_file.exists():
        return False
    try:
        age = time.time() - cache_file.stat().st_mtime
        return age < _CATALOG_CACHE_TTL
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Catalog-first read path with fail-open fallback (Decision 8)
# ---------------------------------------------------------------------------

def _read_catalog_or_scan(catalog_type: str) -> list[dict]:
    """Try catalog first. On any failure, fall back to an empty list.

    Implements the fail-open read path (Design Decision 8): attempt to
    read and validate a catalog JSON file for *catalog_type*.  On any
    failure — missing file, parse error, schema mismatch — log a
    once-per-session warning and return an empty list so the caller can
    fall back to live file scanning.

    Args:
        catalog_type: One of ``"memory"``, ``"diary"``, ``"skills"``,
            or ``"resources"``.

    Returns:
        Catalog entries on success, or an empty list on any failure.
    """
    if catalog_type not in _KNOWN_CATALOG_TYPES:
        logger.debug("Unknown catalog type '%s', returning empty", catalog_type)
        return []

    catalog_file = _resolve_catalog_path(catalog_type)
    warn_key = str(catalog_file)

    try:
        if not catalog_file.exists():
            logger.debug("Catalog file not found: %s", catalog_file)
            return []

        data = json.loads(catalog_file.read_text(encoding="utf-8"))
        return _validate_catalog(data, catalog_type, warn_key)

    except json.JSONDecodeError as e:
        _warn_once(warn_key, f"Catalog {catalog_type} contains invalid JSON: {e}")
        return []
    except OSError as e:
        _warn_once(warn_key, f"Error reading catalog {catalog_type}: {e}")
        return []
    except Exception as e:
        _warn_once(warn_key, f"Unexpected error reading catalog {catalog_type}: {e}")
        return []


def _resolve_catalog_path(catalog_type: str) -> Path:
    """Return the expected filesystem path for a catalog type.

    Filename convention matches what each generator writes via
    ``catalog_filename`` (e.g., ``memory.json`` for MemoryGenerator).
    """
    paths = get_paths()
    return paths.catalogs_dir() / f"{catalog_type}.json"


def _validate_catalog(data: object, catalog_type: str, warn_key: str) -> list[dict]:
    """Validate parsed catalog JSON and return entries, or ``[]`` on failure.

    Checks that *data* is a dict with a matching ``schema_version`` and
    a list-typed ``entries`` field. Logs a once-per-session warning for
    each distinct validation failure.
    """
    if not isinstance(data, dict):
        _warn_once(warn_key, f"Catalog {catalog_type} is not a JSON object, falling back")
        return []

    schema_version = data.get("schema_version")
    if schema_version is None:
        _warn_once(warn_key, f"Catalog {catalog_type} missing schema_version field, falling back")
        return []

    if schema_version != CATALOG_SCHEMA_VERSION:
        _warn_once(
            warn_key,
            f"Catalog {catalog_type} schema version mismatch: "
            f"expected {CATALOG_SCHEMA_VERSION}, got {schema_version}",
        )
        return []

    entries = data.get("entries")
    if entries is None:
        return []
    if not isinstance(entries, list):
        _warn_once(warn_key, f"Catalog {catalog_type} entries is not a list, falling back")
        return []

    return entries


def _warn_once(warn_key: str, message: str) -> None:
    """Log *message* at WARNING level, but only once per session per *warn_key*.

    Subsequent calls with the same key are demoted to DEBUG to avoid
    log spam (Design Decision 8).
    """
    if warn_key in _catalog_warnings_emitted:
        logger.debug("Suppressed repeated warning for %s: %s", warn_key, message)
        return
    _catalog_warnings_emitted.add(warn_key)
    logger.warning(message)


# ---------------------------------------------------------------------------
# Per-project "now" state loading
# ---------------------------------------------------------------------------

def _load_project_state(now_dir: Path, cwd: str) -> str | None:
    """Return the ``now_dir / {project}.md`` file contents for *cwd*, or None.

    The project name is the final path component of *cwd* — matching how
    ``synthesize_now.py`` groups diary entries. Returns ``None`` when
    *cwd* is empty, the directory does not exist, the project file is
    missing, or the file cannot be read. Per-project scoping keeps
    signal-to-noise high: working on DolceEngine should not surface the
    multiplai-plugin status file.
    """
    if not cwd or not now_dir.exists():
        return None
    project = Path(cwd).name
    if not project:
        return None
    project_file = now_dir / f"{project}.md"
    if not project_file.exists():
        return None
    try:
        return project_file.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Failed to read project state file: %s", project_file)
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Context manager main: read stdin, route context, write JSON to stdout."""
    paths = get_paths()
    memory_dir = paths.memory_dir()
    now_dir = paths.now_dir()

    # Read user prompt from stdin (Claude Code hook protocol)
    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        input_data = {}

    cwd = input_data.get("cwd", "") if isinstance(input_data, dict) else ""
    prompt = input_data.get("user_prompt", "") if isinstance(input_data, dict) else ""

    # Intent-domain routing: when the catalog has entries with
    # intent_domains, delegate the pick to the configured router
    # (token_overlap or llm — see lib.memory_router). mtime+size
    # ranking runs only when the router returns no picks (missing
    # catalog, no prompt, or no matches).
    catalog_entries = _read_catalog_or_scan("memory") if prompt else []
    try:
        router = create_router()
        intent_picks = router.select(prompt, catalog_entries)
        if intent_picks:
            logger.info("Router %s picked %d file(s)", router.name, len(intent_picks))
    except NotImplementedError as e:
        logger.warning("Router misconfigured: %s", e)
        intent_picks = []
    except Exception:
        logger.exception("Router call failed; falling back to metadata ranking")
        intent_picks = []

    if intent_picks:
        memory_files: dict[str, str] = {}
        for filename in intent_picks:
            path = memory_dir / filename
            if path.exists():
                try:
                    memory_files[filename] = path.read_text()
                except OSError:
                    logger.warning("Failed to read router-picked memory file: %s", path)
        if not memory_files:
            memory_files = _read_top_memory_files(memory_dir)
    else:
        memory_files = _read_top_memory_files(memory_dir)

    project_state = _load_project_state(now_dir, cwd)

    if not memory_files and not project_state:
        logger.info("No memory files or project state found, skipping context routing")
        result = {"context": "", "memory_files": 0}
        print(json.dumps(result))
        return

    file_count = len(memory_files)
    logger.info(
        "Context manager loaded %d memory files%s",
        file_count,
        " + project state" if project_state else "",
    )

    context_parts = []
    if project_state:
        context_parts.append(f"--- PROJECT STATE ---\n{project_state}")
    for name, content in memory_files.items():
        context_parts.append(f"## {name}\n{content}")

    session_context = "\n\n".join(context_parts)

    result = {
        "context": session_context,
        "memory_files": file_count,
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
