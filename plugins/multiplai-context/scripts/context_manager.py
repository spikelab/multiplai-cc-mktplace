# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core @ git+https://github.com/spikelab/multiplai-core@v0.2"]
# ///
"""Context manager hook for multiplai plugin.

Manages context assembly for user prompts across three corpora —
memory, skills, and resources — using catalog-first reads with
intent-domain routing.

Pipeline:
    1. Read stdin (cwd, prompt, transcript_path)
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

from multiplai_core.paths import get_paths
from multiplai_core.config import read_session_state, write_session_state
from multiplai_core.log_utils import setup_logging, log_event
from multiplai_core.model_client import create_client  # D3: LLM calls via ModelClient abstraction
from lib.memory_router import create_router
from lib.routing_logic import expand_picks
from lib.section_loader import load_picked_content, parse_section_ref
from lib.transcript_helper import read_last_assistant_response
from generators.base import CATALOG_SCHEMA_VERSION
from generators.config import load_catalog_config

logger = setup_logging("context_manager")

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


def _read_top_memory_files(
    memory_dir: Path,
    *,
    max_files: int = 5,
    max_bytes: int = 40_000,
    exclude: set[str] | None = None,
) -> dict[str, str]:
    """Rank memory files by metadata, then read only the top candidates.

    This is the key R2 mitigation: avoid reading all files to stay under
    the 5-second timeout on large memory sets. Two additional guards keep the
    recency fallback (which fires on every prompt when no catalog exists yet)
    from dumping the whole memory corpus each turn:
      - ``exclude`` skips files still within the re-recommendation cooldown,
        so the fallback doesn't re-inject what a recent turn already sent.
      - ``max_bytes`` caps the total injected payload.
    """
    exclude = exclude or set()
    ranked = _rank_memory_files(memory_dir)
    result: dict[str, str] = {}
    total = 0
    for item in ranked:
        if len(result) >= max_files:
            break
        if item.path.name in exclude:
            continue
        try:
            content = item.path.read_text()
        except Exception:
            logger.warning("Failed to read ranked memory file: %s", item.path)
            continue
        if total + len(content.encode("utf-8")) > max_bytes and result:
            break
        result[item.path.name] = content
        total += len(content.encode("utf-8"))
    return result


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


def _build_skills_recommendations(
    cfg, picks: list[str], entries: list[dict]
) -> dict[str, str]:
    """Build lightweight skill recommendations from the catalog.

    Skills are surfaced as *pointers*, not content. Claude Code already
    exposes every skill body via the Skill tool and lists available
    skills, so injecting the full SKILL.md per prompt is redundant and
    heavy. Instead we emit the catalog summary plus the ``/<name>``
    invocation hint so the model knows a relevant skill exists and how
    to call it. No file reads — the catalog is the source of truth.
    """
    if not picks or not cfg.enable_skills:
        return {}
    by_name: dict[str, dict] = {}
    for entry in entries or []:
        key = entry.get("name") or entry.get("source")
        if key:
            by_name[key] = entry
    result: dict[str, str] = {}
    for pick in picks:
        # Skills entries don't use section refs.
        base, _ = parse_section_ref(pick)
        summary = (by_name.get(base) or {}).get("summary", "").strip()
        hint = f"Invoke with /{base} when relevant."
        result[base] = f"{summary}\n{hint}" if summary else hint
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
# Re-recommendation cooldown (turn-based dedup)
# ---------------------------------------------------------------------------


def _filter_cooldown(
    picks: list[str], last_injected: dict, turn_index: int, cooldown: int
) -> tuple[list[str], list[str]]:
    """Split *picks* into (kept, suppressed) by the cooldown window.

    A pick is suppressed when it was injected within the last *cooldown*
    turns (it's already in the conversation). ``turn_index - last <=
    cooldown`` is the suppression test.
    """
    kept: list[str] = []
    suppressed: list[str] = []
    cmap = last_injected if isinstance(last_injected, dict) else {}
    for pick in picks:
        last = cmap.get(pick)
        if isinstance(last, int) and (turn_index - last) <= cooldown:
            suppressed.append(pick)
        else:
            kept.append(pick)
    return kept, suppressed


def _persist_turn_state(
    session_state: dict,
    turn_index: int,
    recent: dict,
    injected_by_corpus: dict[str, dict],
    cooldown: int,
) -> None:
    """Stamp this turn's injections into *recent* and write session state.

    Records ``turn_index`` for every injected key, prunes entries that
    have aged past the cooldown window (bounds file growth), bumps the
    turn counter, and atomically rewrites ``session_state.json``.
    Fail-open: a write error is logged at debug, never raised — the hook
    must not break a prompt over bookkeeping.
    """
    for corpus_type, content in injected_by_corpus.items():
        cmap = recent.get(corpus_type)
        if not isinstance(cmap, dict):
            cmap = recent[corpus_type] = {}
        for key in content:
            cmap[key] = turn_index
        stale = [
            k for k, t in cmap.items()
            if not isinstance(t, int) or (turn_index - t) > cooldown
        ]
        for k in stale:
            del cmap[k]
    session_state["turn_index"] = turn_index
    session_state["recently_injected"] = recent
    # NOTE: turn_index / recently_injected live in a single shared
    # session_state.json. Two sessions running concurrently against the same
    # plugin-data dir can race this read-modify-write and clobber each other's
    # cooldown map — a mild degradation (a file may be re-injected a turn early
    # or suppressed a turn late), not data loss. The severe cross-session bug
    # (deferred diary/learnings markers filed under the wrong session id) is
    # fixed separately in session_end.py / pre_compact.py by trusting the
    # hook-input session id. A full fix here needs per-session state files.
    try:
        write_session_state(get_paths().data_dir(), session_state)
    except Exception:
        logger.debug("Could not persist turn state", exc_info=True)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _emit_result(context: str, result: dict) -> None:
    """Write the hook result to stdout in the shape Claude Code actually reads.

    UserPromptSubmit only injects context from ``hookSpecificOutput.
    additionalContext`` (or plain stdout text) — a bare ``{"context": ...}``
    key is silently ignored, which made routed memory a no-op. We emit
    ``additionalContext`` for Claude Code AND keep the legacy ``context`` /
    ``*_files`` keys (extra keys are ignored by the harness) so existing
    consumers and tests still read the same fields.
    """
    payload = dict(result)
    payload["hookSpecificOutput"] = {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": context,
    }
    print(json.dumps(payload))


def main() -> None:
    """Context manager main: read stdin, route context, write JSON to stdout."""
    paths = get_paths()
    memory_dir = paths.memory_dir()

    # Read hook input from stdin (Claude Code UserPromptSubmit shape)
    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        input_data = {}

    prompt = input_data.get("prompt", "") if isinstance(input_data, dict) else ""
    transcript_path = (
        input_data.get("transcript_path") if isinstance(input_data, dict) else None
    )
    session_id = (
        input_data.get("session_id") if isinstance(input_data, dict) else None
    )

    # Capture the working directory for the diary/now pipeline. UserPromptSubmit
    # reliably carries cwd, so this is the dependable place to record it;
    # SessionEnd reads it back from session_state to tag the diary entry (and
    # thus the project). Best-effort and only written when it changes, so the
    # common steady-state path adds no extra write.
    cwd = input_data.get("cwd", "") if isinstance(input_data, dict) else ""
    if cwd:
        try:
            _ss = read_session_state(paths.data_dir()) or {}
            if _ss.get("cwd") != cwd:
                _ss["cwd"] = cwd
                write_session_state(paths.data_dir(), _ss)
        except Exception:
            logger.debug("Could not persist cwd to session_state", exc_info=True)

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
    router_diag: dict | None = None
    used_fallback = False
    router_ran = False
    if prompt and any(corpora.values()):
        try:
            router = create_router()
            picks_by_corpus = router.select_multi(prompt, last_response, corpora)
            router_ran = True
            logger.info(
                "Router %s picked: memory=%d skills=%d resources=%d",
                router.name,
                len(picks_by_corpus.get("memory", [])),
                len(picks_by_corpus.get("skills", [])),
                len(picks_by_corpus.get("resources", [])),
            )
            # Routing-quality diagnostics (token_overlap only; the LLM
            # router exposes no scores). Machine-readable line consumed
            # by the eval harness and the /health Routing Quality check.
            router_diag = getattr(router, "last_scores", None) or None
            if router_diag:
                _mem = router_diag.get("memory") or {}
                _scored = _mem.get("scored") or []
                _cap = _mem.get("cap", 10)
                # _apply_policy keeps a contiguous top prefix, so the
                # set actually injected is scored[:n_picked]. Emit that
                # — NOT scored[:cap], the raw candidate pool — so
                # /health's live top/floor describe what was injected,
                # not an excluded candidate (the conflation that made
                # the live floor read artificially low).
                _np = _mem.get("n_picked", 0)
                logger.info(
                    "ROUTING_SCORES memory=%s",
                    json.dumps({
                        "picked": [[fn, round(s, 3)] for s, fn in _scored[:_np]],
                        "cap": _cap,
                        "n_candidates": _mem.get("n_candidates", len(_scored)),
                        "n_picked": _np,
                        "capped": _mem.get("capped", False),
                        "floor_excluded": (
                            round(_scored[_np][0], 3)
                            if len(_scored) > _np
                            else None
                        ),
                    }),
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

    # --- Re-recommendation cooldown (turn-based dedup) -------------------
    # Drop picks injected within the last `cooldown` turns: they're
    # already in the conversation, so re-injecting them just wastes
    # context. State (a turn counter + per-file last-injected turn) lives
    # in session_state.json and is reset on PreCompact, so once context
    # is summarized away every file becomes eligible again. Fail-open: a
    # state read/parse error degrades to "no cooldown this turn".
    cooldown = max(0, cfg.recommend_cooldown_turns)
    cooldown_active = bool(prompt) and cooldown > 0
    session_state: dict = {}
    turn_index = 0
    recent: dict = {}
    cooldown_suppressed: dict[str, list[str]] = {}
    # Capture what the router actually picked, before cooldown trims it,
    # so the fallback can tell genuine abstention from cooldown removal.
    router_picked = {ct: bool(picks_by_corpus.get(ct)) for ct in ("memory", "skills", "resources")}
    if cooldown_active:
        try:
            _ss = read_session_state(paths.data_dir())
        except Exception:
            _ss = None
        session_state = _ss if isinstance(_ss, dict) else {}
        turn_index = int(session_state.get("turn_index", 0) or 0) + 1
        _recent = session_state.get("recently_injected")
        recent = _recent if isinstance(_recent, dict) else {}
        for corpus_type in ("memory", "skills", "resources"):
            kept, suppressed = _filter_cooldown(
                picks_by_corpus.get(corpus_type) or [],
                recent.get(corpus_type) or {},
                turn_index, cooldown,
            )
            picks_by_corpus[corpus_type] = kept
            if suppressed:
                cooldown_suppressed[corpus_type] = suppressed
        if any(cooldown_suppressed.values()):
            logger.info(
                "COOLDOWN turn=%d window=%d suppressed=%s",
                turn_index, cooldown,
                json.dumps({k: sorted(v) for k, v in cooldown_suppressed.items()}),
            )

    # Load content per corpus
    memory_content = _load_memory_content(memory_dir, picks_by_corpus.get("memory") or [])
    skills_content = _build_skills_recommendations(
        cfg, picks_by_corpus.get("skills") or [], corpora.get("skills") or []
    )
    resources_content = _load_resources_content(cfg, picks_by_corpus.get("resources") or [])

    # Memory fallback — a safety net for *failure*, not for abstention.
    # A successful router run that returns no memory picks is a
    # deliberate "nothing is relevant" (NONE floor or continuation
    # guard); honoring it means injecting nothing, which is correct.
    # The recency net only fires when the router never ran (exception /
    # misconfig) or it picked files that didn't resolve on disk
    # (catalog↔disk drift) — genuine failures with no ranking signal.
    # Use the *pre-cooldown* pick count for abstention: the router
    # genuinely abstained only if it picked no memory at all. If it
    # picked files that cooldown then trimmed, that's a deliberate
    # "already in context", not abstention — and must not trigger the
    # recency dump either.
    router_abstained = router_ran and not router_picked["memory"]
    mem_on_cooldown = bool(cooldown_suppressed.get("memory")) and not memory_content
    if not memory_content and not router_abstained and not mem_on_cooldown:
        # Don't re-inject files still within the cooldown window, and cap the
        # payload — the recency fallback fires every prompt when no catalog
        # exists yet, so an unbounded dump would flood each turn.
        _mem_recent = recent.get("memory") if isinstance(recent.get("memory"), dict) else {}
        on_cooldown = {
            name for name, t in _mem_recent.items()
            if isinstance(t, int) and (turn_index - t) <= cooldown
        }
        memory_content = _read_top_memory_files(memory_dir, exclude=on_cooldown)
        if memory_content:
            used_fallback = True
            logger.info("FALLBACK memory=%s", json.dumps(sorted(memory_content.keys())))
            log_event(
                "context", "fallback",
                "router matched nothing — fell back to recency-ranked memory → "
                + ", ".join(sorted(memory_content.keys())),
                session_id=session_id,
                files=sorted(memory_content.keys()),
            )

    if not memory_content and not skills_content and not resources_content:
        logger.info("No context to inject")
        # Make abstention visible in the human log: a deliberate
        # "nothing relevant" is the floor/continuation guard working,
        # not a failure — say which, with the top score as evidence.
        _skip_msg = "no context matched this prompt"
        if any(cooldown_suppressed.values()) and not router_abstained:
            _n = sum(len(v) for v in cooldown_suppressed.values())
            _skip_msg = (
                f"all {_n} matched file(s) injected within the last "
                f"{cooldown} turns — on cooldown, nothing injected"
            )
        elif router_abstained:
            _dm = (router_diag or {}).get("memory") or {}
            _ds = _dm.get("scored") or []
            if _dm.get("continuation"):
                _skip_msg = "continuation prompt — context already in conversation, nothing injected"
            elif _ds:
                _skip_msg = (
                    f"router abstained — best memory score {_ds[0][0]:g} "
                    f"below relevance floor ({len(_ds)} cand), nothing injected"
                )
            else:
                _skip_msg = "router abstained — no memory matched, nothing injected"
        log_event(
            "context", "skip", _skip_msg,
            session_id=session_id,
        )
        if cooldown_active:
            _persist_turn_state(session_state, turn_index, recent, {}, cooldown)
        result = {"context": "", "memory_files": 0}
        _emit_result("", result)
        return

    parts: list[str] = []
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
        "Context assembled: memory=%d skills=%d resources=%d",
        corpus_counts["memory"],
        corpus_counts["skills"],
        corpus_counts["resources"],
    )

    # Per-corpus file groups so the activity line says which files are
    # memory vs skills vs resources — a flat list loses that attribution.
    files_by_corpus = {
        "memory": sorted(memory_content),
        "skills": sorted(skills_content),
        "resources": sorted(resources_content),
    }
    injected = files_by_corpus["memory"] + files_by_corpus["skills"] + files_by_corpus["resources"]
    file_groups = [
        f"{corpus}: {', '.join(names)}"
        for corpus, names in files_by_corpus.items()
        if names
    ]
    # Compact routing-quality hint for the human activity log. Scores
    # are read from the *picked* set, not the raw candidate pool: since
    # _apply_policy keeps a contiguous top-of-ranking prefix, the floor
    # is scored[n_picked-1] (the lowest file actually injected) — not
    # scored[cap-1], which would report an excluded file's score.
    # Abstention is reported honestly instead of a fake range.
    score_hint = ""
    if not used_fallback and router_diag:
        _m = router_diag.get("memory") or {}
        _s = _m.get("scored") or []
        _np = _m.get("n_picked", 0)
        if _m.get("continuation"):
            score_hint = " · continuation — nothing injected"
        elif _np <= 0:
            score_hint = (
                f" · no match (best {_s[0][0]:g} < floor) — nothing injected"
                if _s else " · no candidates — nothing injected"
            )
        elif _s:
            _cand = _m.get("n_candidates", len(_s))
            _top = _s[0][0]
            _floor = _s[_np - 1][0]
            _cap_tag = " CAP-HIT" if _m.get("capped") else ""
            score_hint = (
                f" · scores {_top:g}→{_floor:g}{_cap_tag} "
                f"({_np}/{_cand} kept)"
            )
    summary = (
        f"injected {corpus_counts['memory']} memory · "
        f"{corpus_counts['skills']} skills · "
        f"{corpus_counts['resources']} resources"
        + score_hint
        + (f" → {' · '.join(file_groups)}" if file_groups else "")
    )
    log_event(
        "context", "inject", summary,
        session_id=session_id,
        memory=corpus_counts["memory"],
        skills=corpus_counts["skills"],
        resources=corpus_counts["resources"],
        files=injected,
        files_by_corpus=files_by_corpus,
        bytes=len(session_context),
    )

    if cooldown_active:
        _persist_turn_state(
            session_state, turn_index, recent,
            {"memory": memory_content, "skills": skills_content,
             "resources": resources_content},
            cooldown,
        )

    result = {
        "context": session_context,
        # Backward-compat: older consumers read memory_files
        "memory_files": corpus_counts["memory"],
        "skills_files": corpus_counts["skills"],
        "resources_files": corpus_counts["resources"],
        "corpus_counts": corpus_counts,
    }
    _emit_result(session_context, result)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Hook runs on every prompt; never propagate a traceback or
        # non-zero exit, or we break the user's Claude Code session.
        try:
            logger.exception("context_manager hook failed; emitting empty context")
        except Exception:
            pass
        _emit_result("", {
            "context": "",
            "memory_files": 0,
            "skills_files": 0,
            "resources_files": 0,
            "corpus_counts": {"memory": 0, "skills": 0, "resources": 0},
        })
        sys.exit(0)
