"""Evaluation harness for the deep-research pipeline.

Builds a dataset of (query, expected output) pairs from existing research
outputs in RESOURCES/, then scores new pipeline runs against quality metrics:

- coverage: fraction of sub-questions with findings
- source diversity: unique domains / total sources
- finding depth: average findings per source
- format compliance: valid YAML appendix present
- confidence calibration: correlation between tagged confidence and source tier

Usage (library):
    from research_pipeline.eval import build_dataset, score_report
    dataset = build_dataset(Path("RESOURCES"))
    score = score_report(report_text, reference=dataset[0])
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataset model
# ---------------------------------------------------------------------------


@dataclass
class EvalFixture:
    """A single entry in the eval dataset."""

    file: str
    query: str
    research_type: str
    preset: str
    source_count: int
    finding_count: int
    has_yaml_appendix: bool
    key_findings: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    unique_domains: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Dataset building
# ---------------------------------------------------------------------------


YAML_APPENDIX_RE = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)


def extract_yaml_appendix(text: str) -> dict | None:
    """Extract and parse the YAML data appendix from a research output file."""
    match = YAML_APPENDIX_RE.search(text)
    if not match:
        return None
    try:
        return yaml.safe_load(match.group(1))
    except Exception:  # noqa: BLE001
        return None


def parse_research_file(path: Path) -> EvalFixture | None:
    """Parse a single research output file into an EvalFixture."""
    try:
        text = path.read_text()
    except Exception:  # noqa: BLE001
        return None

    appendix = extract_yaml_appendix(text)
    if not appendix:
        return None

    meta = appendix.get("meta", {}) if isinstance(appendix, dict) else {}
    findings = appendix.get("findings", []) if isinstance(appendix, dict) else []
    sources = appendix.get("sources", []) if isinstance(appendix, dict) else []

    if not isinstance(findings, list):
        findings = []
    if not isinstance(sources, list):
        sources = []

    source_urls: list[str] = []
    for s in sources:
        if isinstance(s, dict) and "url" in s:
            source_urls.append(s["url"])
        elif isinstance(s, str):
            source_urls.append(s)

    unique_domains = len({urlparse(u).netloc for u in source_urls if u})

    key_findings: list[str] = []
    for f in findings[:10]:  # top 10 as representative sample
        if isinstance(f, dict) and "fact" in f:
            key_findings.append(f["fact"])

    return EvalFixture(
        file=str(path),
        query=meta.get("query", ""),
        research_type=meta.get("research_type", "general"),
        preset=meta.get("preset", "standard"),
        source_count=len(sources),
        finding_count=len(findings),
        has_yaml_appendix=True,
        key_findings=key_findings,
        sources=source_urls,
        unique_domains=unique_domains,
    )


def build_dataset(resources_dir: Path) -> list[EvalFixture]:
    """Scan RESOURCES/ for research files with YAML appendices."""
    fixtures: list[EvalFixture] = []
    for md_file in resources_dir.rglob("*.md"):
        # Quick filter: filename contains a date pattern
        if not re.search(r"\d{4}-\d{2}-\d{2}", md_file.name):
            continue
        fixture = parse_research_file(md_file)
        if fixture and fixture.has_yaml_appendix:
            fixtures.append(fixture)

    log.info("Built eval dataset: %d fixtures from %s", len(fixtures), resources_dir)
    return fixtures


def write_dataset(fixtures: list[EvalFixture], output: Path) -> None:
    """Write fixtures to a JSONL file."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w") as f:
        for fixture in fixtures:
            f.write(json.dumps(fixture.to_dict()) + "\n")
    log.info("Wrote %d fixtures to %s", len(fixtures), output)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass
class QualityScore:
    """Quality metrics for a single report."""

    has_yaml_appendix: bool = False
    source_count: int = 0
    unique_domains: int = 0
    finding_count: int = 0
    findings_per_source: float = 0.0
    coverage_ratio: float = 0.0  # findings covering sub-questions / total sub-questions
    format_compliance: float = 0.0  # 0-1 how well it matches expected structure
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def score_report(
    report_text: str,
    reference: EvalFixture | None = None,
) -> QualityScore:
    """Score a single report text against quality metrics."""
    score = QualityScore()
    notes: list[str] = []

    # Parse appendix
    appendix = extract_yaml_appendix(report_text)
    if not appendix:
        notes.append("missing YAML appendix")
        score.notes = notes
        return score

    score.has_yaml_appendix = True

    if not isinstance(appendix, dict):
        notes.append("appendix is not a dict")
        score.notes = notes
        return score

    # Source metrics
    sources = appendix.get("sources") or []
    findings = appendix.get("findings") or []
    index = appendix.get("index") or {}

    if isinstance(sources, list):
        score.source_count = len(sources)
        urls = [
            s.get("url", "") if isinstance(s, dict) else ""
            for s in sources
        ]
        score.unique_domains = len({urlparse(u).netloc for u in urls if u})

    if isinstance(findings, list):
        score.finding_count = len(findings)
        if score.source_count > 0:
            score.findings_per_source = len(findings) / score.source_count

    # Coverage — check if 'index.questions_investigated' is present and all
    # listed sub-questions have at least one finding that keywords-match
    questions = (
        index.get("questions_investigated", [])
        if isinstance(index, dict)
        else []
    )
    if isinstance(questions, list) and questions:
        covered = 0
        for q in questions:
            q_text = q if isinstance(q, str) else str(q)
            q_tokens = set(re.findall(r"\b[a-z]{4,}\b", q_text.lower()))
            for f in findings:
                if not isinstance(f, dict):
                    continue
                fact_tokens = set(
                    re.findall(r"\b[a-z]{4,}\b", str(f.get("fact", "")).lower())
                )
                if q_tokens and len(q_tokens & fact_tokens) / len(q_tokens) >= 0.3:
                    covered += 1
                    break
        score.coverage_ratio = covered / len(questions)

    # Format compliance — check for key structural elements
    structural_checks = [
        "meta" in appendix,
        "sources" in appendix,
        "findings" in appendix,
        "# " in report_text,  # at least one top-level heading
        "## " in report_text,  # section headings
    ]
    score.format_compliance = sum(structural_checks) / len(structural_checks)

    # Compare to reference if provided
    if reference:
        if score.source_count < reference.source_count * 0.7:
            notes.append(
                f"source count ({score.source_count}) below reference "
                f"({reference.source_count})"
            )
        if score.finding_count < reference.finding_count * 0.7:
            notes.append(
                f"finding count ({score.finding_count}) below reference "
                f"({reference.finding_count})"
            )

    score.notes = notes
    return score


# ---------------------------------------------------------------------------
# CLI for offline eval building
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Deep research eval harness")
    parser.add_argument("--resources", type=Path, required=True, help="Path to RESOURCES/")
    parser.add_argument("--output", type=Path, required=True, help="Where to write JSONL fixture")
    args = parser.parse_args()

    fixtures = build_dataset(args.resources)
    write_dataset(fixtures, args.output)
    print(f"Built {len(fixtures)} fixtures")


if __name__ == "__main__":
    main()
