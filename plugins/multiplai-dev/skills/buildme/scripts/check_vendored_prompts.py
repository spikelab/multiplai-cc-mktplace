#!/usr/bin/env python3
"""Staleness check for the vendored reviewer prompts.

Run this **by hand before a release**. It is wired to nothing — no hook, no CI,
no import from the pipeline — exactly like the `sdk-tools.d.ts` re-derivation
documented in `build_pipeline/sdk.py`. A borrowed prompt drifts from upstream
silently; a copy nobody can tell has gone stale is the actual failure mode, and
this script is what tells you.

It reads the manifest at `build_pipeline/prompts/vendored/SOURCES.json`,
re-fetches each pinned path from GitHub through `gh api`, and compares the blob
SHA GitHub returns now against the `blob_sha` the manifest recorded. Blob SHAs
are the pin, not the tree SHA and not a branch: a tree SHA moves when any file
under it moves without telling you which, and a branch is not a version.

Exit codes
    0   every pinned blob SHA still matches upstream
    1   at least one file drifted
    2   the manifest is missing, unreadable, or malformed
    3   at least one fetch failed (and nothing drifted)

A fetch failure is never reported as "unchanged". `gh` is already
authenticated; this script never reads, passes, or prints a token.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

DEFAULT_MANIFEST = (
    Path(__file__).resolve().parent
    / "build_pipeline"
    / "prompts"
    / "vendored"
    / "SOURCES.json"
)

# The manifest contract, fixed with the agent that writes SOURCES.json.
REQUIRED_KEYS = ("repo", "path", "blob_sha", "tree_sha", "licence", "modified", "used_by")

# Transient enough to be worth re-issuing the identical request. Anything else
# is a fact about the upstream repo and retrying only repeats the same answer.
RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})

_HTTP_STATUS_RE = re.compile(r"\(HTTP (\d{3})\)")


class ManifestError(Exception):
    """The manifest is missing, unreadable, or does not meet the contract."""


@dataclass(frozen=True)
class FetchError(Exception):
    """One re-fetch failed. Carries the status so the caller can say why."""

    status: int | None
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        where = f"HTTP {self.status}" if self.status is not None else "no HTTP status"
        return f"{where}: {self.message}"


@dataclass(frozen=True)
class VendoredSource:
    repo: str
    path: str
    blob_sha: str
    tree_sha: str
    licence: str
    modified: bool
    used_by: tuple[str, ...]


@dataclass
class CheckResult:
    source: VendoredSource
    status: str  # "ok" | "drift" | "error"
    actual_blob_sha: str | None = None
    error: str | None = None
    hint: str | None = None
    attempts: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.source.repo,
            "path": self.source.path,
            "status": self.status,
            "expected_blob_sha": self.source.blob_sha,
            "actual_blob_sha": self.actual_blob_sha,
            "recorded_tree_sha": self.source.tree_sha,
            "licence": self.source.licence,
            "modified": self.source.modified,
            "used_by": list(self.source.used_by),
            "error": self.error,
            "hint": self.hint,
            "attempts": self.attempts,
        }


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------


def load_manifest(path: Path) -> list[VendoredSource]:
    """Parse SOURCES.json into VendoredSource records.

    Raises ManifestError — never a bare OSError or JSONDecodeError — so the CLI
    can print one clear line instead of a traceback.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ManifestError(
            f"manifest not found: {path}\n"
            "  Nothing is vendored yet, or the path moved. Pass --manifest to "
            "point at it."
        ) from None
    except OSError as exc:
        raise ManifestError(f"manifest unreadable: {path} ({exc})") from None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest is not valid JSON: {path} (line {exc.lineno}: {exc.msg})") from None

    if not isinstance(data, list):
        raise ManifestError(
            f"manifest must be a JSON list of objects, got {type(data).__name__}: {path}"
        )
    if not data:
        raise ManifestError(f"manifest lists no vendored files: {path}")

    sources: list[VendoredSource] = []
    for index, entry in enumerate(data):
        where = f"{path} entry {index}"
        if not isinstance(entry, dict):
            raise ManifestError(f"{where}: expected an object, got {type(entry).__name__}")
        missing = [key for key in REQUIRED_KEYS if key not in entry]
        if missing:
            raise ManifestError(f"{where}: missing required key(s): {', '.join(missing)}")
        used_by = entry["used_by"]
        if not isinstance(used_by, list):
            raise ManifestError(f"{where}: 'used_by' must be a list, got {type(used_by).__name__}")
        sources.append(
            VendoredSource(
                repo=str(entry["repo"]),
                path=str(entry["path"]),
                blob_sha=str(entry["blob_sha"]),
                tree_sha=str(entry["tree_sha"]),
                licence=str(entry["licence"]),
                modified=bool(entry["modified"]),
                used_by=tuple(str(name) for name in used_by),
            )
        )
    return sources


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------


def _classify(returncode: int, stderr: str) -> FetchError:
    """Turn a failed `gh api` run into a FetchError with the status attached."""
    first_line = next((line for line in stderr.splitlines() if line.strip()), "").strip()
    match = _HTTP_STATUS_RE.search(stderr)
    status = int(match.group(1)) if match else None
    return FetchError(status=status, message=first_line or f"gh api exited {returncode}")


def _hint_for(error: FetchError) -> str:
    if error.status == 404:
        return "upstream path moved or was deleted — report the path, do not guess a new one"
    if error.status in (403, 429):
        return "bot wall or rate limit — not retried; re-run later or check `gh` auth"
    if error.status in RETRYABLE_STATUSES:
        return "upstream server error — retried verbatim and still failed"
    if error.status is None:
        return "network, DNS or timeout failure — retried verbatim and still failed"
    return "unexpected status — not retried"


def _is_retryable(error: FetchError) -> bool:
    # No HTTP status at all means the request never got an answer: DNS failure,
    # connection reset, timeout. That is the one case worth re-issuing.
    return error.status is None or error.status in RETRYABLE_STATUSES


def _run_gh(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


def fetch_blob_sha(
    source: VendoredSource,
    *,
    runner: Callable[[list[str]], subprocess.CompletedProcess] = _run_gh,
    retries: int = 2,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, int]:
    """Return (blob_sha, attempts) for one pinned path, via `gh api`.

    `gh` supplies its own credentials; no token is read, passed, or printed
    here. Raises FetchError on failure — never returns a sentinel that a caller
    could mistake for "unchanged".
    """
    argv = [
        "gh",
        "api",
        "-H",
        "Accept: application/vnd.github+json",
        f"repos/{source.repo}/contents/{source.path}",
        "--jq",
        ".sha",
    ]

    last: FetchError | None = None
    for attempt in range(1, retries + 2):
        try:
            proc = runner(argv)
        except FileNotFoundError:
            raise FetchError(
                status=None,
                message="`gh` not found on PATH — install the GitHub CLI to run this check",
            ) from None
        except subprocess.TimeoutExpired:
            last = FetchError(status=None, message="`gh api` timed out after 60s")
        else:
            if proc.returncode == 0:
                sha = (proc.stdout or "").strip()
                if not sha:
                    raise FetchError(status=None, message="`gh api` returned an empty blob sha")
                return sha, attempt
            last = _classify(proc.returncode, proc.stderr or "")

        if not _is_retryable(last):
            raise last
        if attempt <= retries:
            sleep(min(2.0 * attempt, 5.0))

    assert last is not None
    raise last


# --------------------------------------------------------------------------
# Check
# --------------------------------------------------------------------------


def check_sources(
    sources: Iterable[VendoredSource],
    *,
    fetch: Callable[..., tuple[str, int]] = fetch_blob_sha,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    for source in sources:
        try:
            actual, attempts = fetch(source)
        except FetchError as exc:
            results.append(
                CheckResult(
                    source=source,
                    status="error",
                    error=str(exc),
                    hint=_hint_for(exc),
                )
            )
            continue
        results.append(
            CheckResult(
                source=source,
                status="ok" if actual == source.blob_sha else "drift",
                actual_blob_sha=actual,
                attempts=attempts,
            )
        )
    return results


def exit_code_for(results: list[CheckResult]) -> int:
    if any(r.status == "drift" for r in results):
        return 1
    if any(r.status == "error" for r in results):
        return 3
    return 0


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _short(sha: str | None) -> str:
    return (sha or "?")[:12]


def render_text(results: list[CheckResult], manifest: Path) -> str:
    lines = [f"Vendored prompt staleness check — {len(results)} pinned file(s)", f"manifest: {manifest}", ""]
    for r in results:
        if r.status == "ok":
            lines.append(f"  OK     {r.source.repo}/{r.source.path}  {_short(r.source.blob_sha)}")
        elif r.status == "drift":
            lines.append(f"  DRIFT  {r.source.repo}/{r.source.path}")
            lines.append(f"           recorded {r.source.blob_sha}")
            lines.append(f"           upstream {r.actual_blob_sha}")
            if r.source.used_by:
                lines.append(f"           used by: {', '.join(r.source.used_by)}")
            if r.source.modified:
                lines.append("           local copy is modified — re-apply the edits, do not overwrite")
        else:
            lines.append(f"  ERROR  {r.source.repo}/{r.source.path}")
            lines.append(f"           {r.error}")
            lines.append(f"           {r.hint}")

    drifted = [r for r in results if r.status == "drift"]
    errored = [r for r in results if r.status == "error"]
    ok = [r for r in results if r.status == "ok"]
    lines.append("")
    lines.append(f"{len(ok)} unchanged, {len(drifted)} drifted, {len(errored)} not checked")
    if drifted:
        lines.append("Drift is not automatically bad — re-read the upstream file, decide whether the")
        lines.append("change matters, then update the prompt and the recorded blob_sha together.")
    if errored:
        lines.append("A failed fetch is NOT 'unchanged'. Those files were not checked at all.")
    return "\n".join(lines)


def render_json(results: list[CheckResult], manifest: Path) -> str:
    payload = {
        "manifest": str(manifest),
        "checked": len(results),
        "unchanged": sum(1 for r in results if r.status == "ok"),
        "drifted": sum(1 for r in results if r.status == "drift"),
        "errors": sum(1 for r in results if r.status == "error"),
        "exit_code": exit_code_for(results),
        "results": [r.as_dict() for r in results],
    }
    return json.dumps(payload, indent=2)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_vendored_prompts.py",
        description=(
            "Re-fetch every prompt pinned in SOURCES.json and report which ones "
            "drifted from the recorded blob SHA. Run by hand before a release; "
            "it is wired to no hook, no CI, and nothing in the pipeline."
        ),
        epilog=(
            "exit codes: 0 all pinned SHAs still match | 1 something drifted | "
            "2 manifest missing or malformed | 3 a fetch failed. "
            "Uses the already-authenticated `gh`; never reads or prints a token."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"path to SOURCES.json (default: {DEFAULT_MANIFEST})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit a JSON report on stdout instead of the human-readable one",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="re-issue an identical request this many times on a 5xx/DNS/timeout "
        "failure only; 404/403/429 are never retried (default: 2)",
    )
    return parser


def main(argv: list[str] | None = None, *, fetch: Callable[..., tuple[str, int]] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        sources = load_manifest(args.manifest)
    except ManifestError as exc:
        if args.as_json:
            print(json.dumps({"manifest": str(args.manifest), "error": str(exc), "exit_code": 2}, indent=2))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2

    if fetch is None:
        def fetch(source: VendoredSource) -> tuple[str, int]:  # type: ignore[misc]
            return fetch_blob_sha(source, retries=args.retries)

    results = check_sources(sources, fetch=fetch)
    print(render_json(results, args.manifest) if args.as_json else render_text(results, args.manifest))
    return exit_code_for(results)


if __name__ == "__main__":
    sys.exit(main())
