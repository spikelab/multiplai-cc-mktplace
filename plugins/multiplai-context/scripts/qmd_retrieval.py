"""qmd-backed resources retrieval for the context manager.

When ``resources_retrieval == "qmd"`` the context manager bypasses the
catalog+router path for the resources corpus and calls :func:`search`
here instead. qmd (https://github.com/tobi/qmd) maintains a hybrid
BM25 + vector index over the resources directory; this module runs the
queries and fuses the result lists.

Two execution modes (``qmd_mode`` plugin option):

- ``local`` — qmd binary on PATH (native installs; also covers a
  container-local qmd used as a BM25-only fallback).
- ``ssh``   — qmd runs on the host reached over the container→host SSH
  bridge (``qmd_ssh_host``). llama.cpp embedding/generation is ~50x
  faster on Apple Metal than on container CPU, so container setups
  index and query host-side. The index is project-local
  (``<workspace>/.qmd/``) at the same absolute path on both sides.

Search strategies (``qmd_strategy``):

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
import subprocess
import sys
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
    "make get set use using went going go".split()
)


@dataclass(frozen=True)
class QmdTarget:
    """Where and how to run qmd for one search call."""

    workspace: str                     # cwd for qmd (project-local index)
    resources_dir: str                 # maps qmd:// URIs to absolute paths
    collection: str = "resources"
    mode: str = "local"                # "local" | "ssh"
    ssh_host: str = "host.docker.internal"
    strategy: str = "fused"            # "fused" | "hybrid" | "fts"


def target_from_config(cfg, workspace: str) -> QmdTarget:
    """Build a QmdTarget from CatalogConfig + the hook payload's cwd."""
    return QmdTarget(
        workspace=workspace,
        resources_dir=cfg.resources_dir.strip(),
        collection=cfg.qmd_collection,
        mode=cfg.qmd_mode,
        ssh_host=cfg.qmd_ssh_host,
        strategy=cfg.qmd_strategy,
    )


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without qmd)
# ---------------------------------------------------------------------------

def sanitize_query(query: str) -> str:
    """Strip gateway-rejected metacharacters and flatten whitespace."""
    q = _GATEWAY_UNSAFE.sub(" ", query)
    return " ".join(q.split())[:MAX_QUERY_CHARS]


def content_words(text: str) -> list[str]:
    """Deduplicated content words for the BM25 keyword ladder."""
    words = [w.strip(".,;:!?()[]\"'`").lower() for w in text.split()]
    keep = [w for w in words if len(w) > 2 and w not in _STOPWORDS and w.isalnum()]
    seen: set[str] = set()
    return [w for w in keep if not (w in seen or seen.add(w))]


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


def results_to_entries(results: list, target: QmdTarget) -> list[dict]:
    """Filter/dedupe raw qmd results into renderable entries.

    Returns ``[{"path", "title", "score", "snippet"}, ...]`` — best
    first, one entry per file, weak matches dropped.
    """
    seen: set[str] = set()
    entries: list[dict] = []
    for item in results or []:
        if not isinstance(item, dict):
            continue
        score = normalize_score(item)
        if score < MIN_SCORE:
            continue
        path = to_abs_path(item, target)
        if path in seen:  # multiple chunks of the same doc
            continue
        seen.add(path)
        snippet = (item.get("snippet") or item.get("text")
                   or item.get("content") or "").strip()
        entries.append({
            "path": path,
            "title": item.get("title") or os.path.basename(path),
            "score": score,
            "snippet": " ".join(snippet.split())[:SNIPPET_CHARS],
        })
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
        remote = f"cd {target.workspace} && qmd {subcmd} '{q}' {tail}"
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


# ---------------------------------------------------------------------------
# Entry point for the context manager
# ---------------------------------------------------------------------------

def search(prompt: str, target: QmdTarget, runner=run_qmd) -> list[dict]:
    """Retrieve resources entries for *prompt*; ``[]`` on any failure.

    Skips slash commands and tiny prompts. Falls back to the BM25 ladder
    when the configured strategy returns nothing (e.g. embeddings still
    pending, or vsearch unavailable in a local BM25-only install).
    """
    prompt = (prompt or "").strip()
    if not prompt or prompt.startswith("/") or len(prompt) < MIN_PROMPT_CHARS:
        return []
    if not target.resources_dir:
        return []

    flat = sanitize_query(prompt)
    try:
        results = None
        if target.strategy == "hybrid":
            results = runner("query", flat, TIMEOUT_HYBRID, target)
        elif target.strategy == "fused":
            results = fused_search(flat, target, runner)
        if not results:
            results = fts_ladder(flat, target, runner)[:MAX_RESULTS] or None
        return results_to_entries(results or [], target)
    except Exception:
        # Retrieval is a nicety — never let it break the prompt.
        logger.exception("qmd retrieval failed; returning no results")
        return []
