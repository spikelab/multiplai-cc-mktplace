"""Tests for the eval harness — fixture building and report scoring."""

from __future__ import annotations

from pathlib import Path

from research_pipeline.eval import (
    build_dataset,
    extract_yaml_appendix,
    parse_research_file,
    score_report,
)


SAMPLE_REPORT = """# Test Research

**Date:** 2026-04-05 | **Confidence:** high | **Sources used:** 3

## Summary

Some prose summary with citations [Source A](https://a.example).

## Findings

### Finding 1

Content with [Source A](https://a.example).

## Sources

| # | Source | Reputation |
|---|--------|------------|
| 1 | [A](https://a.example) | authoritative |

---

<!-- STRUCTURED DATA — machine-readable, do not edit above this line -->

```yaml
index:
  questions_investigated:
    - "What is httpx performance"
    - "How does React render"
  sources_consulted: 3
  total_findings: 2

meta:
  query: "test query"
  date: "2026-04-05"
  research_type: "general"
  preset: "standard"
  confidence: high

findings:
  - fact: "httpx has good performance with async"
    source: "[A](https://a.example)"
    confidence: high
  - fact: "React renders components via virtual DOM"
    source: "[B](https://b.example)"
    confidence: medium

sources:
  - title: "A"
    url: "https://a.example"
    reputation: authoritative
  - title: "B"
    url: "https://b.example"
    reputation: established
  - title: "C"
    url: "https://c.example"
    reputation: emerging
```
"""


class TestYamlAppendix:
    def test_extract_appendix(self) -> None:
        appendix = extract_yaml_appendix(SAMPLE_REPORT)
        assert appendix is not None
        assert appendix["meta"]["query"] == "test query"
        assert len(appendix["sources"]) == 3

    def test_missing_appendix_returns_none(self) -> None:
        assert extract_yaml_appendix("# Just a title\n\nNo appendix.") is None

    def test_malformed_yaml_returns_none(self) -> None:
        bad = "```yaml\n{not: valid: yaml: at: all\n```"
        assert extract_yaml_appendix(bad) is None


class TestParseResearchFile:
    def test_parses_sample(self, tmp_path: Path) -> None:
        f = tmp_path / "sample-2026-04-05.md"
        f.write_text(SAMPLE_REPORT)
        fixture = parse_research_file(f)
        assert fixture is not None
        assert fixture.query == "test query"
        assert fixture.source_count == 3
        assert fixture.finding_count == 2
        assert fixture.unique_domains == 3
        assert fixture.has_yaml_appendix

    def test_file_without_appendix_returns_none(self, tmp_path: Path) -> None:
        f = tmp_path / "plain-2026-04-05.md"
        f.write_text("# Just a plain doc")
        assert parse_research_file(f) is None


class TestBuildDataset:
    def test_builds_from_dated_files(self, tmp_path: Path) -> None:
        (tmp_path / "research-2026-04-05.md").write_text(SAMPLE_REPORT)
        (tmp_path / "undated.md").write_text("ignored")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "nested-2026-03-10.md").write_text(SAMPLE_REPORT)

        fixtures = build_dataset(tmp_path)
        assert len(fixtures) == 2


class TestScoreReport:
    def test_scores_sample_report(self) -> None:
        score = score_report(SAMPLE_REPORT)
        assert score.has_yaml_appendix
        assert score.source_count == 3
        assert score.finding_count == 2
        assert score.unique_domains == 3
        assert score.format_compliance == 1.0
        # Both questions get a keyword match in the findings
        assert score.coverage_ratio > 0

    def test_scores_report_without_appendix(self) -> None:
        score = score_report("# No appendix here")
        assert not score.has_yaml_appendix
        assert "missing YAML appendix" in score.notes
