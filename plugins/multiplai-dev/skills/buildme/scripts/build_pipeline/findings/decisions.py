"""Choose which of a re-review's findings `--auto` posts.

`write_decisions` reads the review's `findings.json` and writes
`buildme-decisions.json` beside it, in the format `review post --decisions`
reads (`{<id>: {"decision": "accept"}}`): of the findings the review lists,
every Code finding, and every Tests finding that cites a source file that is
not a test file; with *severities*, only those of the severities named (an
epic's fixer passes the ones the epic blocks on, so it never runs a round
for a finding the epic would merge with). A Tests finding on a test file,
and every Docs finding, are not posted; the fix loop acts on what is
posted, and those are not defects a code fix answers. A finding the review does not list (a repeat of one the
person rejected, a low-value one, and in `critical` mode one not rated as
breaking) is never posted, as it was not before the decisions file.

The file lives in the review's own folder under the data directory, never in
the PR's worktree, so it is never committed and never blocks a later round.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..code_analysis import _is_source, is_test_file

DECISIONS_FILE = "buildme-decisions.json"

# What `review_pipeline.render.listed` lists, copied: buildme does not import
# review_pipeline. Statuses shown, assess labels hidden, and the impacts
# `--mode critical` keeps.
_SHOWN_STATUSES = ("confirmed", "unverifiable")
_HIDDEN_LABELS = ("repeat", "low-value")
_CRITICAL_IMPACTS = ("breaks-users", "breaks-business")

# Test files by name or directory. The engine's `_TEST_PATH_RE`
# (`tdd_engine.py`) covers Python, Go, Swift and JS/TS; this adds the JVM,
# .NET, Ruby, Elixir and PHP naming conventions and a `spec/` directory.
_MORE_TEST_PATHS = re.compile(
    r"(^|/)("
    r"[^/]+(Test|Tests|IT)\.(java|kt|scala|cs)"          # JVM, .NET
    r"|[^/]+_(spec|test)\.(rb|exs?)"                     # Ruby, Elixir
    r"|[^/]+Test\.php"                                   # PHPUnit
    r")$"
    r"|(^|/)(spec|__tests__|test|tests|Tests)/"
)


def _section(finding: dict) -> str:
    """`code`, `tests` or `docs`, as the review skill files a finding
    (`review_pipeline.models.section_of`): no topic is `code`."""
    topic = finding.get("topic")
    return topic if topic in ("tests", "docs") else "code"


def _listed(finding: dict, mode: str | None) -> bool:
    """Whether the review lists *finding* (`review_pipeline.render.listed`)."""
    label = str((finding.get("assessment") or {}).get("label") or "")
    if finding.get("status") not in _SHOWN_STATUSES or label in _HIDDEN_LABELS:
        return False
    return mode != "critical" or finding.get("impact") in _CRITICAL_IMPACTS


def _is_test_path(path: str) -> bool:
    from ..tdd_engine import _TEST_PATH_RE

    return bool(is_test_file(path) or _TEST_PATH_RE.search(path) or _MORE_TEST_PATHS.search(path))


def _accepted(finding: dict, mode: str | None) -> bool:
    if not _listed(finding, mode):
        return False
    section = _section(finding)
    if section == "code":
        return True
    path = str(finding.get("file") or "")
    return section == "tests" and bool(path) and _is_source(path) and not _is_test_path(path)


def write_decisions(review_dir: str | Path, severities: tuple[str, ...] | None = None) -> Path:
    """Write `<review_dir>/buildme-decisions.json` and return its path; with
    *severities*, a finding of another severity is not chosen.
    Raises `FileNotFoundError` when `findings.json` is missing and
    `ValueError` when it cannot be read as the review's findings, each naming
    the file; nothing is written then."""
    source = Path(review_dir) / "findings.json"
    if not source.is_file():
        raise FileNotFoundError(f"no findings.json at {source}")
    try:
        doc = json.loads(source.read_text(encoding="utf-8"))
        mode = doc.get("mode")
        chosen = {str(f["id"]): {"decision": "accept"} for f in doc["findings"]
                  if _accepted(f, mode) and (severities is None or f.get("severity") in severities)}
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError, AttributeError) as e:
        raise ValueError(f"cannot read {source}: {type(e).__name__}: {e}") from e
    path = Path(review_dir) / DECISIONS_FILE
    path.write_text(json.dumps(chosen, indent=2) + "\n", encoding="utf-8")
    return path
