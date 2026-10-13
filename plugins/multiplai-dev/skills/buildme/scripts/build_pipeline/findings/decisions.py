"""Choose which of a re-review's findings `--auto` posts.

`write_decisions` reads the review's `findings.json` and writes
`buildme-decisions.json` beside it, in the format `review post --decisions`
reads (`{<id>: {"decision": "accept"}}`): every Code finding, and every Tests
finding that cites a source file that is not a test file. A Tests finding on
a test file, and every Docs finding, are not posted; the fix loop acts on
what is posted, and those are not defects a code fix answers.

The file lives in the review's own folder under the data directory, never in
the PR's worktree, so it is never committed and never blocks a later round.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..code_analysis import _is_source, is_test_file

DECISIONS_FILE = "buildme-decisions.json"


def _section(finding: dict) -> str:
    """`code`, `tests` or `docs`, as the review skill files a finding
    (`review_pipeline.models.section_of`): no topic is `code`."""
    topic = finding.get("topic")
    return topic if topic in ("tests", "docs") else "code"


def _accepted(finding: dict) -> bool:
    section = _section(finding)
    if section == "code":
        return True
    path = str(finding.get("file") or "")
    return section == "tests" and bool(path) and _is_source(path) and not is_test_file(path)


def write_decisions(review_dir: str | Path) -> Path:
    """Write `<review_dir>/buildme-decisions.json` and return its path.
    Raises `FileNotFoundError` when `findings.json` is missing and
    `ValueError` when it cannot be read as the review's findings, each naming
    the file; nothing is written then."""
    source = Path(review_dir) / "findings.json"
    if not source.is_file():
        raise FileNotFoundError(f"no findings.json at {source}")
    try:
        findings = json.loads(source.read_text(encoding="utf-8"))["findings"]
        chosen = {str(f["id"]): {"decision": "accept"} for f in findings if _accepted(f)}
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError) as e:
        raise ValueError(f"cannot read {source}: {type(e).__name__}: {e}") from e
    path = Path(review_dir) / DECISIONS_FILE
    path.write_text(json.dumps(chosen, indent=2) + "\n", encoding="utf-8")
    return path
