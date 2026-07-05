"""Eval harness: Model x Effort A/B test.

Feeds the same fixed content to each model×effort combo via a single
llm_call per condition. No pipeline, no web search, no fetching.
Just: "given this content, write a research report" — 6 SDK calls total.

Usage:
    python eval_effort_matrix.py \
      --query "What are the key differences between SQLite WAL mode and rollback journal mode?" \
      --output-dir /tmp/effort-eval \
      [--models claude-sonnet-4-6 claude-opus-4-6] \
      [--efforts low medium high] \
      [--skip-judge] [--judge-model claude-opus-4-6]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

# Ensure research_pipeline is importable
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from research_pipeline.sdk import (
    LLMCallUsage,
    get_accumulated_usage,
    llm_call,
    reset_accumulated_usage,
)


# ---------------------------------------------------------------------------
# Fixture content — pre-fetched web pages
# ---------------------------------------------------------------------------

FIXTURE_CONTENT = """
## Source 1: Write-Ahead Logging (WAL) in SQLite (sqlite.org/wal.html)

Write-Ahead Logging (WAL) is an alternative to the default rollback journal mechanism for implementing atomic commit and rollback in SQLite, available since version 3.7.0.

### How WAL Works
- Preserves original content in database file
- Appends changes to separate WAL file
- COMMIT occurs when special commit record is appended to WAL
- Allows readers to continue using original unaltered database while changes are committed
- Multiple transactions can append to single WAL file

### Checkpointing
Checkpointing transfers transactions from the WAL file back into the original database. By default, SQLite automatically checkpoints when WAL file reaches 1000 pages (~4MB) or last database connection closes.

### Concurrency
- Readers check WAL first for pages; if not found, read from database file
- Use "wal-index" (shared memory) to quickly locate pages in WAL
- Writers merely append new content to end of WAL file
- Only one writer at a time
- Checkpoint can run concurrently with readers

### Advantages
1. Significantly faster in most scenarios
2. Better concurrency — readers don't block writers, writers don't block readers
3. More sequential disk I/O
4. Fewer fsync() operations

### Disadvantages
1. All processes must be on same machine — doesn't work over network filesystems (requires shared memory via -shm file)
2. Limited multi-database atomicity
3. Cannot change page_size after entering WAL mode
4. 1-2% slower reads for read-heavy applications
5. Creates additional "-wal" and "-shm" files
6. Checkpointing overhead
7. Works best with smaller transactions

### Performance
- Write: very fast — one sequential write vs two for rollback journal
- Read: deteriorates as WAL grows (time to check WAL proportional to size)
- Checkpoint: slower than writes, requires sync and seeking

### WAL-Reset Bug (Fixed 3.51.3, 2026-03-13)
Critical bug in versions 3.7.0-3.51.2: rare data race with simultaneous write/checkpoint from separate threads can cause database corruption. Low occurrence rate but serious consequences.

## Source 2: File Locking And Concurrency In SQLite Version 3 (sqlite.org/lockingv3.html)

### Five Locking States
1. UNLOCKED — no locks, cannot read/write
2. SHARED — can read, multiple processes can hold simultaneously
3. RESERVED — intends to write, only one allowed, coexists with SHARED
4. PENDING — waiting for SHARED locks to clear, no new SHARED permitted
5. EXCLUSIVE — required to write, only one allowed, no other locks coexist

### Rollback Journal Mechanism
When a process changes the database:
- Records original content in a rollback journal file ([database]-journal)
- Records initial database size for truncation on rollback
- Original page content written to journal BEFORE modifying database
- Journal deletion (or truncation) IS the commit instant
- If crash before deletion: hot journal detected on next open, triggers automatic rollback

### Write Process
1. Obtain SHARED lock (for reading)
2. Acquire RESERVED lock (signals intent to write)
3. Create rollback journal with original pages
4. Hold changes in memory
5. Flush journal to disk
6. Obtain EXCLUSIVE lock (may wait for SHARED locks to clear)
7. Write changes to database file
8. Flush database changes to disk
9. Delete journal file (THIS is the commit)
10. Drop locks

### Writer Starvation Prevention
PENDING lock allows existing readers to continue but prevents NEW readers. Writer eventually gets EXCLUSIVE when current readers finish.

### Corruption Risks
- POSIX advisory locks buggy on NFS — avoid network filesystems
- fsync() may not truly flush on some hardware
- Hot journal deletion by administrators breaks recovery
- Hard/soft links to database files prevent journal discovery

### Multi-Database Transactions
Uses a super-journal that aggregates individual database journals. Super-journal deletion is the commit instant for multi-database transactions.
"""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Condition:
    model: str
    effort: str
    label: str


@dataclass
class RunResult:
    condition: Condition
    report: str = ""
    elapsed_s: float = 0.0
    usage: LLMCallUsage = field(default_factory=LLMCallUsage)
    error: str | None = None


@dataclass
class JudgeScore:
    label: str
    accuracy: int = 0
    insight_depth: int = 0
    completeness: int = 0
    structure: int = 0
    actionability: int = 0
    total: int = 0
    notes: str = ""


# ---------------------------------------------------------------------------
# Synthesis prompt (single call per condition)
# ---------------------------------------------------------------------------

SYNTHESIS_PROMPT = """You are a research analyst. Given the source material below, write a research report answering this query:

QUERY: {query}

SOURCE MATERIAL:
{content}

Write a well-structured markdown report that:
1. Directly answers the query with specific technical details
2. Compares and contrasts the two approaches
3. Provides clear recommendations for when to use each
4. Cites specific facts from the sources
5. Notes any caveats, edge cases, or risks

Be thorough but concise. Include a summary table if helpful."""


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------

JUDGE_PROMPT = """You are evaluating {n} research reports that all answer the same query using the same source material.
Each report was produced by a different model/effort configuration, but you don't know which.

QUERY: {query}

Score each report on these dimensions (1 = poor, 5 = excellent):
1. **Accuracy**: Facts correct? Any errors or hallucinations vs the source material?
2. **Insight depth**: Goes beyond surface-level? Explains mechanisms, trade-offs, edge cases?
3. **Completeness**: Covers the key aspects? Missing major angles from the sources?
4. **Structure**: Well-organized? Clear headings, logical flow, easy to scan?
5. **Actionability**: Reader knows what to DO after reading? Clear recommendations?

{reports}

Return a JSON array with one object per report:
```json
[
  {{"report_id": "A", "accuracy": 4, "insight_depth": 3, "completeness": 4, "structure": 5, "actionability": 3, "notes": "Brief explanation"}},
  ...
]
```

Return ONLY the JSON array in a fenced code block."""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run_condition(query: str, condition: Condition) -> RunResult:
    """Run a single synthesis call and capture output + cost."""
    reset_accumulated_usage()
    prompt = SYNTHESIS_PROMPT.format(query=query, content=FIXTURE_CONTENT)

    start = time.monotonic()
    try:
        report = await llm_call(
            prompt,
            model=condition.model,
            effort=condition.effort,
        )
        elapsed = time.monotonic() - start
        usage = get_accumulated_usage()
        return RunResult(
            condition=condition,
            report=report,
            elapsed_s=elapsed,
            usage=usage,
        )
    except Exception as e:
        elapsed = time.monotonic() - start
        return RunResult(
            condition=condition,
            elapsed_s=elapsed,
            usage=get_accumulated_usage(),
            error=str(e),
        )


async def run_judge(
    results: list[RunResult],
    query: str,
    judge_model: str,
) -> list[JudgeScore]:
    """Blinded LLM judge scores all reports."""
    # Randomize and label A-F
    indexed = list(enumerate(results))
    random.shuffle(indexed)
    label_map: dict[str, str] = {}

    reports_text = ""
    for i, (_, result) in enumerate(indexed):
        letter = chr(65 + i)
        label_map[letter] = result.condition.label
        text = result.report[:8000] if result.report else "(FAILED — no report generated)"
        reports_text += f"\n\n--- REPORT {letter} ---\n\n{text}\n"

    prompt = JUDGE_PROMPT.format(
        n=len(results),
        query=query,
        reports=reports_text,
    )

    from research_pipeline.sdk import extract_json

    raw = await llm_call(prompt, model=judge_model, effort="high")
    data = extract_json(raw)
    if not isinstance(data, list):
        data = [data]

    scores: list[JudgeScore] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        rid = item.get("report_id", "?")
        real_label = label_map.get(rid, f"unknown-{rid}")
        scores.append(JudgeScore(
            label=real_label,
            accuracy=item.get("accuracy", 0),
            insight_depth=item.get("insight_depth", 0),
            completeness=item.get("completeness", 0),
            structure=item.get("structure", 0),
            actionability=item.get("actionability", 0),
            total=sum(item.get(k, 0) for k in ["accuracy", "insight_depth", "completeness", "structure", "actionability"]),
            notes=item.get("notes", ""),
        ))
    return scores


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_results(
    output_dir: Path,
    query: str,
    results: list[RunResult],
    scores: list[JudgeScore] | None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save individual reports
    for r in results:
        report_path = output_dir / f"{r.condition.label}.md"
        report_path.write_text(r.report or "(no report)")

    # Build score lookup
    score_map: dict[str, JudgeScore] = {}
    if scores:
        score_map = {s.label: s for s in scores}

    # Markdown table
    lines = [
        f"# Model x Effort Eval",
        f"**Query:** {query}",
        f"**Date:** {date.today().isoformat()}",
        f"**Fixture:** 2 SQLite docs (WAL + locking/rollback journal)",
        "",
        "## Results",
        "",
        "| Condition | Acc | Depth | Complete | Structure | Action | **Total** | Cost | Tokens (in/out) | Time |",
        "|-----------|-----|-------|----------|-----------|--------|-----------|------|-----------------|------|",
    ]

    total_cost = 0.0
    for r in sorted(results, key=lambda x: x.condition.label):
        s = score_map.get(r.condition.label)
        total_cost += r.usage.cost_usd
        tok = f"{r.usage.input_tokens:,}/{r.usage.output_tokens:,}"
        if s:
            lines.append(
                f"| {r.condition.label} | {s.accuracy} | {s.insight_depth} | {s.completeness} "
                f"| {s.structure} | {s.actionability} | **{s.total}** "
                f"| ${r.usage.cost_usd:.4f} | {tok} | {r.elapsed_s:.0f}s |"
            )
        else:
            status = "FAILED" if r.error else "no judge"
            lines.append(
                f"| {r.condition.label} | - | - | - | - | - | {status} "
                f"| ${r.usage.cost_usd:.4f} | {tok} | {r.elapsed_s:.0f}s |"
            )

    lines.append("")
    lines.append(f"**Total cost:** ${total_cost:.4f}")

    # Quality per dollar
    if scores:
        lines.extend(["", "## Quality per Dollar", ""])
        for s in sorted(scores, key=lambda x: x.total, reverse=True):
            r = next((r for r in results if r.condition.label == s.label), None)
            cost = r.usage.cost_usd if r else 0
            qpd = f"{s.total / cost:.0f}" if cost > 0 else "∞"
            lines.append(f"- **{s.label}**: {s.total}/25 quality, ${cost:.4f} → {qpd} pts/$")

    # Judge notes
    if scores:
        lines.extend(["", "## Judge Notes", ""])
        for s in sorted(scores, key=lambda x: x.label):
            lines.append(f"- **{s.label}**: {s.notes}")

    md = "\n".join(lines) + "\n"
    (output_dir / "results.md").write_text(md)

    # JSON
    json_data = {
        "query": query,
        "date": date.today().isoformat(),
        "conditions": [
            {
                "label": r.condition.label,
                "model": r.condition.model,
                "effort": r.condition.effort,
                "elapsed_s": r.elapsed_s,
                "cost_usd": r.usage.cost_usd,
                "input_tokens": r.usage.input_tokens,
                "output_tokens": r.usage.output_tokens,
                "error": r.error,
                "score": asdict(score_map[r.condition.label]) if r.condition.label in score_map else None,
            }
            for r in results
        ],
    }
    (output_dir / "results.json").write_text(json.dumps(json_data, indent=2) + "\n")

    print(md)
    log.info("Results written to %s", output_dir)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser(
        prog="eval_effort_matrix",
        description="Model x Effort A/B test — direct LLM calls, no pipeline",
    )
    parser.add_argument("--query", required=True, help="Research question")
    parser.add_argument("--output-dir", required=True, type=Path, help="Output directory")
    parser.add_argument("--judge-model", default="claude-opus-4-6", help="Model for the judge")
    parser.add_argument("--models", nargs="+", default=["claude-sonnet-4-6", "claude-opus-4-6"])
    parser.add_argument("--efforts", nargs="+", default=["low", "medium", "high"],
                        choices=["low", "medium", "high"])
    parser.add_argument("--skip-judge", action="store_true", help="Skip LLM judge")
    args = parser.parse_args()

    # Build matrix
    conditions = [
        Condition(
            model=model,
            effort=effort,
            label=f"{model.split('-')[1]}-{effort}",  # "sonnet-low", "opus-high"
        )
        for model in args.models
        for effort in args.efforts
    ]

    print(f"Eval matrix: {len(conditions)} conditions ({len(args.models)} models x {len(args.efforts)} efforts)")
    for c in conditions:
        print(f"  - {c.label}")
    print()

    # Run conditions sequentially
    results: list[RunResult] = []
    for c in conditions:
        print(f"{'=' * 60}")
        print(f"Running: {c.label} ({c.model} @ {c.effort})")
        print(f"{'=' * 60}")

        result = await run_condition(args.query, c)
        results.append(result)

        if result.error:
            print(f"  FAILED: {result.error}")
        else:
            print(f"  OK: {len(result.report)} chars, {result.elapsed_s:.1f}s, ${result.usage.cost_usd:.4f}")
        print()

    # Judge
    scores: list[JudgeScore] | None = None
    valid = [r for r in results if not r.error]
    if valid and not args.skip_judge:
        print(f"{'=' * 60}")
        print(f"Running blinded judge ({args.judge_model})")
        print(f"{'=' * 60}")
        try:
            scores = await run_judge(valid, args.query, args.judge_model)
        except Exception as e:
            log.error("Judge failed: %s", e)

    # Output
    write_results(args.output_dir, args.query, results, scores)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
