"""qmd-backed resources retrieval for the context manager.

When ``resources_retrieval == "qmd"`` the context manager bypasses the
catalog+router path for the resources corpus and calls :func:`search`
here instead. qmd (https://github.com/tobi/qmd) maintains a hybrid
BM25 + vector index over the resources directory; this module queries it.

Three execution modes (``qmd_mode`` plugin option):

- ``http`` — POST an authored, typed query to a resident ``qmd mcp
  --http`` daemon on the host (``qmd_http_url``). Preferred mode: qmd
  itself does the fusion + rerank, models stay warm in VRAM (no 12s
  cold start per prompt), and the request is a JSON array — no shell, so
  none of the quoting/newline limits of the ssh bridge apply. We author
  the lexical arm ourselves (IDF-rare terms) instead of pasting the raw
  sentence, which is what tobi's skill prescribes and what keeps qmd's
  position-aware score blend honest. ``qmd_strategy`` is ignored here.
- ``local`` — qmd binary on PATH (native installs; also covers a
  container-local qmd used as a BM25-only fallback).
- ``ssh``   — qmd runs on the host reached over the container→host SSH
  bridge (``qmd_ssh_host``). llama.cpp embedding/generation is ~50x
  faster on Apple Metal than on container CPU, so container setups
  index and query host-side. The index is project-local
  (``<workspace>/.qmd/``) at the same absolute path on both sides.

Search strategies (``qmd_strategy``, ``local``/``ssh`` only):

- ``fused``  — vsearch + BM25 keyword ladder merged with reciprocal-rank
  fusion, no LLM rerank (default: ~1-2s on the host).
- ``hybrid`` — ``qmd query``: expansion + rerank; best quality, adds
  seconds.
- ``fts``    — BM25 keyword ladder only (no embeddings required).

Fail-open by design: any error, timeout, or missing qmd returns ``[]``
so the prompt is never blocked. Subprocesses run in their own process
group and the whole group is killed on timeout — the qmd bin wrapper
spawns a node grandchild that outlives a plain child kill and burns CPU
indefinitely.
"""

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.log_utils import setup_logging

logger = setup_logging("qmd_retrieval")

MAX_RESULTS = 5
MIN_SCORE = 0.30          # drop weak matches rather than inject noise
SNIPPET_CHARS = 500
TIMEOUT_HYBRID = 25       # qmd query (expansion + rerank)
TIMEOUT_VEC = 15          # qmd vsearch (query embedding only)
TIMEOUT_FTS = 5           # qmd search (BM25)
RRF_K = 60                # standard reciprocal-rank-fusion constant
SSH_CONNECT_TIMEOUT = 3
HTTP_TIMEOUT = 10         # POST /query round-trip (warm rerank ~6s)
DEFAULT_CANDIDATE_LIMIT = 10   # docs sent to the reranker (latency dial)
MAX_LEX_TERMS = 3         # IDF-rarest content words fed to the lexical arm

# Minimum prompt length worth a retrieval round-trip; shorter prompts
# ("yes", "go on") carry no retrievable signal.
MIN_PROMPT_CHARS = 12
MAX_QUERY_CHARS = 500

# The SSH gateway denies any command string containing shell
# metacharacters or newlines, and the query travels inside single
# quotes — strip all of that. Applied in local mode too: identical
# inputs keep both modes on the same behavior.
_GATEWAY_UNSAFE = re.compile(r"[;|&<>`$()'\"\\\n\r]")

_STOPWORDS = frozenset(
    "a an the is are was were be been do does did have has had i you we it "
    "this that these those what which who how when where why should could "
    "would can will my your our their his her its of in on at to for with "
    "from by about into over under and or not no if then than as so just "
    "me us them there here please help want need know tell show find look "
    "make get set use using went going go "
    # Intent/quantifier fillers that carried no retrieval signal yet slipped
    # into the lexical arm — e.g. the query "I want to learn more about
    # coffee" was searching "learn more coffee" and ANDing the doc away.
    "learn learns learning learned more most understand understands "
    "understanding understood explain explains explaining explained".split()
)


@dataclass(frozen=True)
class QmdTarget:
    """Where and how to run qmd for one search call."""

    workspace: str                     # cwd for qmd (project-local index)
    resources_dir: str                 # maps qmd:// URIs to absolute paths
    collection: str = "resources"
    mode: str = "local"                # "http" | "local" | "ssh"
    ssh_host: str = "host.docker.internal"
    strategy: str = "fused"            # "fused" | "hybrid" | "fts" (local/ssh)
    http_url: str = "http://host.docker.internal:8181"  # qmd mcp --http daemon
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT      # docs reranked (http)
    min_score: float = MIN_SCORE       # weak-match cutoff, all modes


def target_from_config(cfg, workspace: str) -> QmdTarget:
    """Build a QmdTarget from CatalogConfig + the hook payload's cwd."""
    return QmdTarget(
        workspace=workspace,
        resources_dir=cfg.resources_dir.strip(),
        collection=cfg.qmd_collection,
        mode=cfg.qmd_mode,
        ssh_host=cfg.qmd_ssh_host,
        strategy=cfg.qmd_strategy,
        http_url=cfg.qmd_http_url,
        candidate_limit=cfg.qmd_candidate_limit,
        min_score=cfg.qmd_min_score,
    )


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without qmd)
# ---------------------------------------------------------------------------

def sanitize_query(query: str) -> str:
    """Strip gateway-rejected metacharacters and flatten whitespace."""
    q = _GATEWAY_UNSAFE.sub(" ", query)
    return " ".join(q.split())[:MAX_QUERY_CHARS]


def _gateway_safe(value: str) -> bool:
    """True if *value* carries no gateway-rejected metacharacter or newline.

    Used to vet the non-query pieces (workspace path, collection name)
    before they are interpolated into the SSH remote command string. The
    query itself is neutralized by :func:`sanitize_query`; these values
    can't be blindly stripped (a path is meaningful), so we refuse to
    build a command around an unsafe one instead.
    """
    return not _GATEWAY_UNSAFE.search(value)


def content_words(text: str) -> list[str]:
    """Deduplicated content words for the BM25 keyword ladder."""
    words = [w.strip(".,;:!?()[]\"'`").lower() for w in text.split()]
    keep = [w for w in words if len(w) > 2 and w not in _STOPWORDS and w.isalnum()]
    seen: set[str] = set()
    return [w for w in keep if not (w in seen or seen.add(w))]


# ---------------------------------------------------------------------------
# Typed-query authoring (http mode)
#
# We do NOT paste the raw prompt into qmd. The lexical arm ANDs its terms
# and carries 2x weight in qmd's RRF, so a full sentence (stopwords and all)
# either matches nothing or elects a junk doc to rank 1 — which the
# position-aware blend then protects. Instead we hand qmd a typed query:
# a lexical arm of only the IDF-rarest content words, plus a vector arm
# carrying the whole prompt. See INBOX/qmd-http-rewrite-plan.
# ---------------------------------------------------------------------------

def flatten_query(text: str) -> str:
    """Collapse whitespace and bound length; keep the text otherwise intact.

    The vector arm wants natural language, and http mode has no shell to
    protect (unlike :func:`sanitize_query`), so nothing is stripped.
    """
    return " ".join((text or "").split())[:MAX_QUERY_CHARS]


def index_db_path(target: QmdTarget) -> Path:
    """Path to the project-local qmd sqlite index on the shared mount."""
    return Path(target.workspace) / ".qmd" / "index.sqlite"


def doc_frequencies(terms: list[str], target: QmdTarget) -> dict[str, int | None] | None:
    """Document frequency per term from the FTS index, or None if unreachable.

    Read-only, single connection, short timeout. A term that trips the FTS
    query parser maps to ``None`` (unknown DF) rather than raising. Returns
    None (not ``{}``) when the index file itself can't be opened, so callers
    can distinguish "no index" from "term absent".
    """
    if not terms:
        return {}
    db = index_db_path(target)
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return None
    out: dict[str, int | None] = {}
    try:
        for term in terms:
            try:
                row = conn.execute(
                    "SELECT count(*) FROM documents_fts WHERE documents_fts MATCH ?",
                    (f'"{term}"',),   # double-quote → literal token, never an FTS operator
                ).fetchone()
                out[term] = int(row[0]) if row else 0
            except sqlite3.Error:
                out[term] = None
    finally:
        conn.close()
    return out


def lexical_terms(prompt: str, target: QmdTarget,
                  max_terms: int = MAX_LEX_TERMS) -> list[str]:
    """The IDF-rarest content words of *prompt* for the lexical arm.

    Ordered by document frequency ascending (rarest first) so ``coffee``
    (12/311) beats ``more`` (248/311). Terms absent from the corpus (DF 0)
    are dropped — ANDed into the lexical arm they would zero it out. When
    the index can't be read, degrade to stopword-filtered prompt order.
    """
    words = content_words(prompt)
    if not words:
        return []
    dfs = doc_frequencies(words, target)
    if dfs is None:                       # index unreachable
        return words[:max_terms]
    scored: list[tuple[int, int, str]] = []
    for i, w in enumerate(words):
        df = dfs.get(w)
        if df == 0:                       # not in corpus → contributes nothing
            continue
        # unknown DF (parser-tripping token) → treat as rare but keep it.
        scored.append((df if df is not None else 1, i, w))
    scored.sort(key=lambda t: (t[0], t[1]))   # DF asc, then prompt order
    return [w for _, _, w in scored[:max_terms]]


def build_searches(prompt: str, target: QmdTarget) -> list[dict]:
    """Author the typed ``searches`` array qmd's REST /query expects.

    Always a vector arm over the full prompt; a lexical arm is prepended
    only when there are rare terms worth ANDing. A prompt with no in-corpus
    content words (a genuine negative control) goes vector-only.
    """
    searches = [{"type": "vec", "query": flatten_query(prompt)}]
    lex = lexical_terms(prompt, target)
    if lex:
        searches.insert(0, {"type": "lex", "query": " ".join(lex)})
    return searches


def normalize_score(item: dict) -> float:
    """Map a qmd score to [0, 1] — some subcommands emit percentages."""
    s = item.get("score", 0)
    try:
        s = float(s)
    except (TypeError, ValueError):
        return 0.0
    return s / 100.0 if s > 1.0 else s


def to_abs_path(item: dict, target: QmdTarget) -> str:
    """Map a qmd result to an absolute file path Claude can Read."""
    uri = item.get("file") or item.get("uri") or item.get("path") or ""
    prefix = f"qmd://{target.collection}/"
    if uri.startswith(prefix):
        return f"{target.resources_dir}/{uri[len(prefix):]}"
    if uri.startswith("/"):
        return uri
    return f"{target.resources_dir}/{uri}"


def interleave_ladder_steps(steps: list[list[dict]], target: QmdTarget) -> list[dict]:
    """Round-robin merge of keyword-ladder result lists, deduped by file.

    Interleaving by rank (each step's #1 before any step's #2) keeps one
    catch-all doc from burying a precise narrow-query hit.
    """
    merged: list[dict] = []
    seen: set[str] = set()
    for rank in range(max((len(s) for s in steps), default=0)):
        for step in steps:
            if rank < len(step):
                path = to_abs_path(step[rank], target)
                if path not in seen:
                    seen.add(path)
                    merged.append(step[rank])
    return merged


def rrf_fuse(
    vec: list[dict],
    fts: list[dict],
    target: QmdTarget,
    max_results: int = MAX_RESULTS,
) -> list[dict]:
    """Fuse vector + BM25 result lists by reciprocal rank (per file).

    Each result keeps the metadata of its highest-scoring occurrence and
    carries that score forward for MIN_SCORE filtering downstream.
    """
    fused: dict[str, dict] = {}
    for source in (vec, fts):
        rank = 0
        for item in source:
            if not isinstance(item, dict):
                continue
            path = to_abs_path(item, target)
            if path not in fused:
                fused[path] = {"item": item, "rrf": 0.0, "best": 0.0}
            entry = fused[path]
            entry["rrf"] += 1.0 / (RRF_K + rank)
            score = normalize_score(item)
            if score > entry["best"]:
                entry["best"] = score
                entry["item"] = item  # keep metadata from the stronger hit
            rank += 1
    ordered = sorted(fused.values(), key=lambda e: e["rrf"], reverse=True)
    results = []
    for e in ordered[:max_results]:
        item = dict(e["item"])
        item["score"] = e["best"]
        results.append(item)
    return results


def results_to_entries(results: list, target: QmdTarget,
                       min_score: float = MIN_SCORE) -> list[dict]:
    """Filter/dedupe raw qmd results into renderable entries.

    Returns ``[{"path", "title", "score", "snippet"[, "line"]}, ...]`` —
    best first, one entry per file, weak matches (< *min_score*) dropped.
    ``line`` is the start of the best-matching chunk (qmd matches chunks,
    not whole docs); omitted when qmd doesn't report one.
    """
    seen: set[str] = set()
    entries: list[dict] = []
    for item in results or []:
        if not isinstance(item, dict):
            continue
        score = normalize_score(item)
        if score < min_score:
            continue
        path = to_abs_path(item, target)
        if path in seen:  # multiple chunks of the same doc
            continue
        seen.add(path)
        snippet = (item.get("snippet") or item.get("text")
                   or item.get("content") or "").strip()
        entry = {
            "path": path,
            "title": item.get("title") or os.path.basename(path),
            "score": score,
            "snippet": " ".join(snippet.split())[:SNIPPET_CHARS],
        }
        line = item.get("line")
        if isinstance(line, int) and not isinstance(line, bool) and line > 0:
            entry["line"] = line
        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# qmd execution
# ---------------------------------------------------------------------------

def build_argv(subcmd: str, query: str, target: QmdTarget) -> list[str] | None:
    """Argv for a qmd search command, via SSH bridge or local binary."""
    q = sanitize_query(query)
    if not q:
        return None
    tail = f"-c {target.collection} --json -n {MAX_RESULTS}"
    if target.mode == "ssh":
        # Everything interpolated into the host-interpreted remote string
        # must be vetted, not just the query. workspace comes from the hook
        # cwd and collection from config; a metacharacter in either would
        # break out of the intended command. Refuse to build the command
        # rather than risk it (fail-open: no retrieval this turn). Both are
        # then single-quoted so ordinary spaces survive — and because
        # _GATEWAY_UNSAFE rejects the single quote itself, a vetted value
        # cannot close the quoting early.
        if not (_gateway_safe(target.workspace) and _gateway_safe(target.collection)):
            logger.warning(
                "qmd ssh target unsafe (workspace/collection has shell "
                "metacharacters); skipping retrieval"
            )
            return None
        remote = (
            f"cd '{target.workspace}' && "
            f"qmd {subcmd} '{q}' -c '{target.collection}' --json -n {MAX_RESULTS}"
        )
        return [
            "ssh", "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
            target.ssh_host, remote,
        ]
    qmd = shutil.which("qmd", path=f"{os.path.expanduser('~')}/.bun/bin:"
                       + os.environ.get("PATH", ""))
    if not qmd:
        return None
    return [qmd, subcmd, q, *tail.split()]


def run_qmd(subcmd: str, query: str, timeout: int, target: QmdTarget) -> list | None:
    """Run one qmd search command; return parsed JSON results or None."""
    argv = build_argv(subcmd, query, target)
    if argv is None:
        return None
    try:
        proc = subprocess.Popen(
            argv, cwd=target.workspace or None, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
    except OSError as e:
        logger.warning("qmd %s spawn failed: %s", subcmd, type(e).__name__)
        return None
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
        logger.warning("qmd %s timeout after %ds (group killed)", subcmd, timeout)
        return None
    if proc.returncode != 0:
        logger.warning("qmd %s rc=%d: %s", subcmd, proc.returncode, stderr[:300])
        return None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        logger.warning("qmd %s bad json: %s", subcmd, stdout[:200])
        return None
    if isinstance(data, dict):
        data = data.get("results", data.get("documents", []))
    return data if isinstance(data, list) else None


def fts_ladder(flat: str, target: QmdTarget, runner=run_qmd) -> list[dict]:
    """BM25 keyword ladder: qmd search ANDs terms, so a full NL question
    usually matches nothing — retry with progressively fewer content
    words, then round-robin merge the steps."""
    words = content_words(flat)
    steps: list[list[dict]] = []
    for attempt in (words[:4], words[:3], words[:2]):
        if not attempt:
            break
        step = runner("search", " ".join(attempt), TIMEOUT_FTS, target) or []
        if step:
            steps.append([i for i in step if isinstance(i, dict)])
    return interleave_ladder_steps(steps, target)


def fused_search(flat: str, target: QmdTarget, runner=run_qmd) -> list | None:
    """Vector + BM25 fused by reciprocal rank (per file), no LLM rerank."""
    vec = runner("vsearch", flat, TIMEOUT_VEC, target) or []
    fts = fts_ladder(flat, target, runner)
    if not vec and not fts:
        return None
    return rrf_fuse(vec, fts, target)


def http_search(searches: list[dict], intent: str, target: QmdTarget) -> list | None:
    """POST an authored typed query to the qmd daemon's REST /query endpoint.

    qmd owns the fusion + rerank; we send typed ``searches`` (never a raw
    ``query`` string, which would re-enable auto-expansion and the poisoned
    2x-weighted raw-sentence lexical arm). ``minScore`` is left at 0 so the
    weak-match cutoff stays with :func:`results_to_entries`. Returns the raw
    result list, or None on any transport/parse failure (fail-open).
    """
    payload = {
        "searches": searches,
        "intent": intent,               # steers rerank + snippet extraction
        "collections": [target.collection],
        "limit": MAX_RESULTS,
        "candidateLimit": target.candidate_limit,
        "minScore": 0.0,
        "rerank": True,
    }
    url = f"{target.http_url.rstrip('/')}/query"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logger.warning("qmd http query failed (%s): %s", url, type(e).__name__)
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("qmd http bad json: %s", raw[:200])
        return None
    if isinstance(data, dict):
        data = data.get("results", data.get("documents", []))
    return data if isinstance(data, list) else None


# ---------------------------------------------------------------------------
# Entry point for the context manager
# ---------------------------------------------------------------------------

def search(prompt: str, target: QmdTarget, runner=run_qmd,
           http_runner=http_search) -> list[dict]:
    """Retrieve resources entries for *prompt*; ``[]`` on any failure.

    In ``http`` mode we author a typed query and let the daemon fuse +
    rerank. In ``local``/``ssh`` mode we run the configured strategy and
    fall back to the BM25 ladder when it returns nothing (e.g. embeddings
    still pending, or vsearch unavailable in a BM25-only install). Slash
    commands and tiny prompts are skipped.
    """
    prompt = (prompt or "").strip()
    if not prompt or prompt.startswith("/") or len(prompt) < MIN_PROMPT_CHARS:
        return []
    if not target.resources_dir:
        return []

    try:
        if target.mode == "http":
            searches = build_searches(prompt, target)
            results = http_runner(searches, prompt, target)
            return results_to_entries(results or [], target, target.min_score)

        flat = sanitize_query(prompt)
        results = None
        if target.strategy == "hybrid":
            results = runner("query", flat, TIMEOUT_HYBRID, target)
        elif target.strategy == "fused":
            results = fused_search(flat, target, runner)
        if not results:
            results = fts_ladder(flat, target, runner)[:MAX_RESULTS] or None
        return results_to_entries(results or [], target, target.min_score)
    except Exception:
        # Retrieval is a nicety — never let it break the prompt.
        logger.exception("qmd retrieval failed; returning no results")
        return []
