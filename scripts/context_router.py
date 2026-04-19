"""Context router hook for multiplai plugin.

Routes user prompts through memory files for context enrichment.
Uses path resolver for all file locations. Memory files are ranked by
metadata (recency + size) and only top candidates are read, staying
within the 5-second hook timeout (R2 mitigation).
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

logger = setup_logging("context_router")

# Catalog cache staleness threshold in seconds (15 minutes)
_CATALOG_CACHE_TTL = 900
_CATALOG_FILENAME = "memory_catalog.json"


@dataclass
class RankedFile:
    """A memory file ranked by metadata."""
    path: Path
    size: int
    mtime: float
    score: float


def _iter_markdown_files(directory: Path):
    """Yield markdown (.md) files in *directory*, skipping non-files."""
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
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Context router main: read stdin, route context, write JSON to stdout."""
    paths = get_paths()
    memory_dir = paths.memory_dir()

    # Read user prompt from stdin (Claude Code hook protocol)
    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        input_data = {}

    # Rank and read only top memory files to stay under the 5-second
    # timeout (R2 mitigation — metadata-first ranking, not full reads).
    memory_files = _read_top_memory_files(memory_dir)

    if not memory_files:
        logger.info("No memory files found, skipping context routing")
        result = {"context": "", "memory_files": 0}
        print(json.dumps(result))
        return

    file_count = len(memory_files)
    logger.info("Context router loaded %d memory files", file_count)

    # Build context from memory files
    context_parts = []
    for name, content in memory_files.items():
        context_parts.append(f"## {name}\n{content}")

    session_context = "\n\n".join(context_parts)

    # Output result as JSON to stdout
    result = {
        "context": session_context,
        "memory_files": file_count,
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
