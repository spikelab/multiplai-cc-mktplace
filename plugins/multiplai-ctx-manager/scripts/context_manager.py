"""Context manager hook for multiplai plugin.

Manages context assembly for user prompts across three corpora —
memory, skills, and resources — using catalog-first reads with
intent-domain routing.

Pipeline:
    1. Read stdin (cwd, user_prompt, transcript_path)
    2. Extract last assistant response from transcript (disambiguation)
    3. Load each enabled corpus catalog (gated on plugin options)
    4. Run multi-corpus router → per-corpus filename picks
    5. Expand picks via bundle + co_retrieve_for metadata
    6. Read picked file contents (with section_loader for memory)
    7. Emit a single JSON object with the assembled context

Catalog-first reads (Design Decision 8): when a valid catalog exists,
entries are read from the catalog. Missing or corrupt catalogs fall
back to live scanning (fail-open). Warnings emit once per session.

Memory remains the only corpus with a metadata-fallback path
(_read_top_memory_files) so prompts still get useful context even
without a current catalog. Skills and resources are catalog-only.
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
from lib.routing_logic import expand_picks
from lib.section_loader import load_picked_content, parse_section_ref
from lib.transcript_helper import read_last_assistant_response
from generators.base import CATALOG_SCHEMA_VERSION
from generators.config import load_catalog_config

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
# Multi-corpus catalog loading
# ---------------------------------------------------------------------------

def _load_corpora(cfg) -> dict[str, list[dict]]:
    """Read all enabled catalogs.

    Memory is always loaded. Skills and resources are gated by their
    respective ``enable_*`` plugin options — when disabled, the corpus
    is treated as empty (no routing, no content loading).
    """
    corpora: dict[str, list[dict]] = {
        "memory": _read_catalog_or_scan("memory"),
        "skills": [],
        "resources": [],
    }
    if cfg.enable_skills:
        corpora["skills"] = _read_catalog_or_scan("skills")
    if cfg.enable_resources and cfg.resources_dir.strip():
        corpora["resources"] = _read_catalog_or_scan("resources")
    return corpora


# ---------------------------------------------------------------------------
# Per-corpus content loading
# ---------------------------------------------------------------------------


def _load_memory_content(memory_dir: Path, picks: list[str]) -> dict[str, str]:
    """Read picked memory files; honor ``filename#Section`` for partial loads.

    Returns ``{display_name: content}`` where ``display_name`` is the
    raw pick (e.g., ``"file.md#Section"``) so the assembled context
    shows which slice was loaded.
    """
    result: dict[str, str] = {}
    for pick in picks:
        base, _ = parse_section_ref(pick)
        path = memory_dir / base
        if not path.exists():
            logger.debug("Picked memory file missing: %s", path)
            continue
        try:
            text = path.read_text()
        except OSError:
            logger.warning("Failed to read router-picked memory file: %s", path)
            continue
        # load_picked_content returns (filename, content_or_section)
        _, content = load_picked_content(pick, text)
        result[pick] = content
    return result


def _load_skills_content(cfg, picks: list[str]) -> dict[str, str]:
    """Read picked skill instruction files from configured skills_dir."""
    if not picks or not cfg.enable_skills:
        return {}
    skills_dir = Path(cfg.skills_dir).expanduser()
    if not skills_dir.exists():
        return {}
    result: dict[str, str] = {}
    for pick in picks:
        # Skills entries don't use section refs; they're small docs already.
        base, _ = parse_section_ref(pick)
        path = skills_dir / base
        if not path.exists():
            logger.debug("Picked skill file missing: %s", path)
            continue
        try:
            result[base] = path.read_text()
        except OSError:
            logger.warning("Failed to read picked skill file: %s", path)
    return result


def _load_resources_content(cfg, picks: list[str]) -> dict[str, str]:
    """Read picked resource files from configured resources_dir."""
    if not picks or not cfg.enable_resources or not cfg.resources_dir.strip():
        return {}
    resources_dir = Path(cfg.resources_dir).expanduser()
    if not resources_dir.exists():
        return {}
    result: dict[str, str] = {}
    for pick in picks:
        base, _ = parse_section_ref(pick)
        path = resources_dir / base
        if not path.exists():
            logger.debug("Picked resource file missing: %s", path)
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            logger.warning("Failed to read picked resource file: %s", path)
            continue
        _, content = load_picked_content(pick, text)
        result[pick] = content
    return result


def _render_corpus_section(label: str, files: dict[str, str]) -> str:
    """Render one corpus's loaded files as a labeled markdown block."""
    parts = [f"=== {label} ==="]
    for name, content in files.items():
        parts.append(f"## {name}\n{content}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Context manager main: read stdin, route context, write JSON to stdout."""
    paths = get_paths()
    memory_dir = paths.memory_dir()
    now_dir = paths.now_dir()

    # Read hook input from stdin (Claude Code UserPromptSubmit shape)
    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        input_data = {}

    cwd = input_data.get("cwd", "") if isinstance(input_data, dict) else ""
    prompt = input_data.get("user_prompt", "") if isinstance(input_data, dict) else ""
    transcript_path = (
        input_data.get("transcript_path") if isinstance(input_data, dict) else None
    )

    # Last-assistant-response disambiguation. Failure modes already
    # encoded in read_last_assistant_response (returns None on any error).
    last_response = (
        read_last_assistant_response(transcript_path) if prompt else None
    )

    # Plugin-options-driven config (which corpora are enabled, paths, etc.)
    cfg = load_catalog_config()

    # Load all enabled corpora
    corpora = _load_corpora(cfg) if prompt else {"memory": [], "skills": [], "resources": []}

    # Multi-corpus router pass
    picks_by_corpus: dict[str, list[str]] = {"memory": [], "skills": [], "resources": []}
    if prompt and any(corpora.values()):
        try:
            router = create_router()
            picks_by_corpus = router.select_multi(prompt, last_response, corpora)
            logger.info(
                "Router %s picked: memory=%d skills=%d resources=%d",
                router.name,
                len(picks_by_corpus.get("memory", [])),
                len(picks_by_corpus.get("skills", [])),
                len(picks_by_corpus.get("resources", [])),
            )
        except NotImplementedError as e:
            logger.warning("Router misconfigured: %s", e)
        except Exception:
            logger.exception("Router call failed; per-corpus picks will be empty")

    # Bundle + co_retrieve_for expansion (per-corpus, against that corpus's catalog)
    for corpus_type in ("memory", "skills", "resources"):
        picks = picks_by_corpus.get(corpus_type) or []
        if picks:
            picks_by_corpus[corpus_type] = expand_picks(picks, corpora.get(corpus_type) or [])

    # Log per-file routing decisions (post-expansion) for health audit analytics.
    logger.info(
        "ROUTING memory=%s skills=%s resources=%s",
        json.dumps(sorted(picks_by_corpus.get("memory") or [])),
        json.dumps(sorted(picks_by_corpus.get("skills") or [])),
        json.dumps(sorted(picks_by_corpus.get("resources") or [])),
    )

    # Load content per corpus
    memory_content = _load_memory_content(memory_dir, picks_by_corpus.get("memory") or [])
    skills_content = _load_skills_content(cfg, picks_by_corpus.get("skills") or [])
    resources_content = _load_resources_content(cfg, picks_by_corpus.get("resources") or [])

    # Memory fallback: if nothing picked OR none of the picks resolved on
    # disk, fall back to metadata-ranked top-N. Skills/resources stay
    # catalog-only — no metadata fallback (no obvious ranking signal).
    if not memory_content:
        memory_content = _read_top_memory_files(memory_dir)
        if memory_content:
            logger.info("FALLBACK memory=%s", json.dumps(sorted(memory_content.keys())))

    # Per-project "now" state (cwd-scoped)
    project_state = _load_project_state(now_dir, cwd)

    if not memory_content and not skills_content and not resources_content and not project_state:
        logger.info("No context to inject")
        result = {"context": "", "memory_files": 0}
        print(json.dumps(result))
        return

    parts: list[str] = []
    if project_state:
        parts.append(f"--- PROJECT STATE ---\n{project_state}")
    if memory_content:
        parts.append(_render_corpus_section("MEMORY", memory_content))
    if skills_content:
        parts.append(_render_corpus_section("SKILLS", skills_content))
    if resources_content:
        parts.append(_render_corpus_section("RESOURCES", resources_content))

    session_context = "\n\n".join(parts)

    corpus_counts = {
        "memory": len(memory_content),
        "skills": len(skills_content),
        "resources": len(resources_content),
    }
    logger.info(
        "Context assembled: memory=%d skills=%d resources=%d%s",
        corpus_counts["memory"],
        corpus_counts["skills"],
        corpus_counts["resources"],
        " + project state" if project_state else "",
    )

    result = {
        "context": session_context,
        # Backward-compat: older consumers read memory_files
        "memory_files": corpus_counts["memory"],
        "skills_files": corpus_counts["skills"],
        "resources_files": corpus_counts["resources"],
        "corpus_counts": corpus_counts,
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
