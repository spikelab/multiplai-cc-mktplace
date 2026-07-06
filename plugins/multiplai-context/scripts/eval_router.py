# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core @ git+https://github.com/spikelab/multiplai-core@v0.5.0"]
# ///
"""Standalone routing-quality eval for the live memory_router.

Replaces the retired ``run-context-router-eval.py`` (which imported a
since-removed ``hooks/context-router.py``). This calls the *live*
``memory_router`` the context manager uses, against the real memory
catalog, and writes a JSON snapshot the /health skill reports and the
memory-health-audit skill tracks longitudinally.

It does NOT make LLM calls under the default ``token_overlap``
strategy — safe to run anytime. Under ``--strategy llm`` it makes one
model call per case (cost lives with whoever runs it).

Usage:
    python eval_router.py                       # token_overlap, real catalog, golden sets
    python eval_router.py --strategy llm        # semantic router (costs N calls)
    python eval_router.py --cases a.jsonl b.jsonl
    python eval_router.py --catalog /path/to/memory.json --k 10
    python eval_router.py --quiet               # metrics only, no per-case lines

Case schema (JSONL, one object per line):
    {id, intent, prompt, last_response, expected_files,
     expected_none, unexpected_files, rationale}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.paths import get_paths  # noqa: E402


# ---------------------------------------------------------------------------
# Case loading + catalog
# ---------------------------------------------------------------------------


def _load_cases(paths: list[Path]) -> list[dict]:
    cases: list[dict] = []
    for p in paths:
        if not p.exists():
            raise SystemExit(f"cases file not found: {p}")
        for n, line in enumerate(p.read_text().splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(f"{p}:{n}: bad JSON: {e}")
    return cases


def _default_case_paths() -> list[Path]:
    """Locate golden case sets in the user's eval directory.

    Looks in ``$MULTIPLAI_ROUTER_EVALS_DIR`` if set, else ``<workspace>/evals``.
    The eval sets are user-supplied (this is a diagnostic harness, not a
    shipped dataset), so an empty result is normal — the caller then asks for
    ``--cases`` explicitly.
    """
    env_dir = os.environ.get("MULTIPLAI_ROUTER_EVALS_DIR")
    if env_dir:
        base = Path(env_dir)
    else:
        workspace = get_paths().data_dir().parent.parent  # .multiplai/data -> workspace
        base = workspace / "evals"
    found = [
        base / "memory-retrieval-cases.jsonl",
        base / "memory-retrieval-holdout-cases.jsonl",
    ]
    return [p for p in found if p.exists()]


def _load_catalog(catalog_path: Path) -> list[dict]:
    data = json.loads(catalog_path.read_text())
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise SystemExit(f"catalog {catalog_path} has no 'entries' list")
    return entries


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass
class Metrics:
    total: int = 0
    # NONE handling — the token_overlap weak spot.
    none_expected: int = 0
    none_correct: int = 0  # correctly returned empty
    # Retrieval cases (expected_none == False).
    retr_expected: int = 0
    recall_num: int = 0
    recall_den: int = 0
    precision_relevant: int = 0  # retrieved minus unexpected hits
    precision_total: int = 0  # all retrieved
    fp_checks: int = 0
    fp_hits: int = 0
    # Did the right file make the top-k at all? (precision@k proxy)
    hit_at_k: int = 0
    rank_sum: int = 0  # 1-based rank of first expected file; 0 if missed
    rank_n: int = 0
    # Degeneracy signal: how often the cap is binding + how wide the pool.
    capped: int = 0
    cand_sum: int = 0
    cand_n: int = 0
    failures: list[str] = field(default_factory=list)


def _evaluate(metrics: Metrics, case: dict, picked: list[str], n_candidates: int, cap: int) -> bool:
    expected = case.get("expected_files") or []
    unexpected = set(case.get("unexpected_files") or [])
    none_expected = bool(case.get("expected_none"))
    picked_set = set(picked)
    metrics.total += 1

    if none_expected:
        metrics.none_expected += 1
        ok = not picked
        if ok:
            metrics.none_correct += 1
        else:
            metrics.failures.append(
                f"{case['id']}: expected NONE, got {len(picked)} ({picked[:4]})"
            )
        return ok

    metrics.retr_expected += 1
    # Recall.
    exp_set = set(expected)
    metrics.recall_den += len(exp_set)
    metrics.recall_num += len(exp_set & picked_set)
    # Precision proxy (only unexpected_files count against us — matches
    # the legacy runner: borderline files in neither list are neutral).
    irrelevant = picked_set & unexpected
    metrics.precision_total += len(picked)
    metrics.precision_relevant += len(picked) - len(irrelevant)
    metrics.fp_checks += len(unexpected)
    metrics.fp_hits += len(irrelevant)
    # Rank of first expected file.
    metrics.cand_n += 1
    metrics.cand_sum += n_candidates  # raw matched pool (informative)
    if len(picked) >= cap:            # saturation = the *returned* set
        metrics.capped += 1
    first_rank = 0
    for i, f in enumerate(picked, 1):
        if f in exp_set:
            first_rank = i
            break
    metrics.rank_n += 1
    if first_rank:
        metrics.hit_at_k += 1
        metrics.rank_sum += first_rank
    miss = exp_set - picked_set
    ok = not irrelevant and not miss
    if not ok:
        why = []
        if irrelevant:
            why.append(f"unexpected {sorted(irrelevant)}")
        if miss:
            why.append(f"missed {sorted(miss)}")
        metrics.failures.append(f"{case['id']}: {'; '.join(why)}")
    return ok


def _pct(num: int, den: int) -> float:
    return round(num / den * 100, 1) if den else 0.0


def _summary(m: Metrics, passed: int) -> dict:
    return {
        "total_cases": m.total,
        "pass_rate_pct": _pct(passed, m.total),
        "none_accuracy_pct": _pct(m.none_correct, m.none_expected),
        "none_cases": m.none_expected,
        "recall_pct": _pct(m.recall_num, m.recall_den),
        "precision_pct": _pct(m.precision_relevant, m.precision_total),
        "false_positive_pct": _pct(m.fp_hits, m.fp_checks),
        "hit_at_k_pct": _pct(m.hit_at_k, m.rank_n),
        "mean_first_rank": round(m.rank_sum / m.hit_at_k, 2) if m.hit_at_k else None,
        # Degeneracy: high cap-saturation + wide pools == low-signal routing.
        "cap_saturation_pct": _pct(m.capped, m.cand_n),
        "mean_candidates": round(m.cand_sum / m.cand_n, 1) if m.cand_n else 0.0,
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run(cases: list[dict], catalog: list[dict], k: int, quiet: bool) -> dict:
    from lib.memory_router import create_router

    corpora_base = {"memory": catalog, "skills": [], "resources": []}
    metrics = Metrics()
    passed = 0
    t0 = time.time()

    for case in cases:
        router = create_router()  # fresh: last_scores is per-instance
        picks = router.select_multi(
            case["prompt"],
            case.get("last_response") or None,
            {**corpora_base},
            max_files_per_corpus=k,
        )
        picked = picks.get("memory") or []
        diag = (getattr(router, "last_scores", {}) or {}).get("memory") or {}
        n_candidates = diag.get("n_candidates", len(picked))
        ok = _evaluate(metrics, case, picked, n_candidates, k)
        if ok:
            passed += 1
        if not quiet:
            mark = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
            print(f"  {mark}  {case['id']}: {case.get('intent','')}")

    summary = _summary(metrics, passed)
    summary["strategy"] = create_router().name
    summary["k"] = k
    summary["elapsed_s"] = round(time.time() - t0, 2)
    summary["failures"] = metrics.failures
    return summary


def _write_snapshot(summary: dict, case_files: list[Path]) -> Path:
    out_dir = get_paths().data_dir() / "router-eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record = {
        "generated_at": stamp,
        "case_files": [str(p) for p in case_files],
        **summary,
    }
    (out_dir / "latest.json").write_text(json.dumps(record, indent=2))
    # Dated copy for longitudinal tracking (memory-health-audit reads these).
    (out_dir / f"{time.strftime('%Y-%m-%d', time.gmtime())}.json").write_text(
        json.dumps(record, indent=2)
    )
    return out_dir / "latest.json"


def main() -> None:
    ap = argparse.ArgumentParser(description="Routing-quality eval for the live memory_router")
    ap.add_argument("--cases", nargs="*", type=Path, help="Golden JSONL files (default: personal+holdout)")
    ap.add_argument("--catalog", type=Path, help="memory catalog JSON (default: live memory.json)")
    ap.add_argument("--k", type=int, default=10, help="max files per corpus (default 10)")
    ap.add_argument("--strategy", choices=["token_overlap", "llm"], help="override router strategy for this run")
    ap.add_argument("--quiet", action="store_true", help="metrics only")
    ap.add_argument("--no-write", action="store_true", help="don't write the snapshot file")
    args = ap.parse_args()

    if args.strategy:
        os.environ["CLAUDE_PLUGIN_OPTION_memory_router"] = args.strategy

    case_files = args.cases or _default_case_paths()
    if not case_files:
        raise SystemExit(
            "no golden case files found. This is a diagnostic harness that needs "
            "user-supplied cases: pass --cases <file.jsonl> [...], or place "
            "memory-retrieval-cases.jsonl under <workspace>/evals/ (or set "
            "MULTIPLAI_ROUTER_EVALS_DIR)."
        )
    cases = _load_cases(case_files)

    catalog_path = args.catalog or (get_paths().catalogs_dir() / "memory.json")
    catalog = _load_catalog(catalog_path)

    print(
        f"eval: {len(cases)} cases · catalog {catalog_path.name} "
        f"({len(catalog)} entries) · strategy "
        f"{args.strategy or os.environ.get('CLAUDE_PLUGIN_OPTION_memory_router','token_overlap')} "
        f"· k={args.k}\n"
    )
    summary = run(cases, catalog, args.k, args.quiet)

    print("\n" + "=" * 56)
    for key in (
        "strategy", "total_cases", "pass_rate_pct", "none_accuracy_pct",
        "recall_pct", "precision_pct", "false_positive_pct",
        "hit_at_k_pct", "mean_first_rank",
        "cap_saturation_pct", "mean_candidates", "elapsed_s",
    ):
        print(f"  {key:>22}: {summary[key]}")
    print("=" * 56)

    if not args.no_write:
        path = _write_snapshot(summary, case_files)
        print(f"snapshot: {path}")


if __name__ == "__main__":
    main()
