"""Shared plumbing for the expensive fleet collectors: subprocess + cache."""

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Every subprocess this package runs is bounded. A `git` call against a repo on
# a stalled network mount, or a `gh` call against a flaky connection, must cost
# the digest a few seconds and one "unreachable" line — never the whole run.
DEFAULT_TIMEOUT = 5.0

# How many repos to query at once. Measured: `gh pr list` is ~1.5-2s per repo,
# and 25 repos serial is ~45s — unusable for a command whose entire point is to
# be run at the moment you are walking away. Eight workers puts it under 6s.
# Higher buys little (GitHub rate-limits, and git is disk-bound) and risks
# secondary rate limits.
MAX_WORKERS = 8


@dataclass
class Ran:
    """The outcome of one bounded subprocess call."""

    ok: bool
    out: str = ""
    err: str = ""

    @property
    def lines(self) -> list[str]:
        return [ln for ln in self.out.splitlines() if ln.strip()]


def run(
    args: list[str],
    *,
    cwd: Path | str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Ran:
    """Run *args*, never raise, never inherit stdin.

    ``stdin=DEVNULL`` matters more than it looks: ``git`` and ``gh`` will
    happily block forever on a credential prompt, and this may run from a
    non-interactive context. A blocked prompt is indistinguishable from a hang,
    so we make it a fast failure instead.
    """
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        return Ran(False, err=f"{args[0]}: not found")
    except subprocess.TimeoutExpired:
        return Ran(False, err=f"{args[0]}: timed out after {timeout:g}s")
    except OSError as exc:  # pragma: no cover - platform-specific
        return Ran(False, err=f"{args[0]}: {exc}")
    if proc.returncode != 0:
        return Ran(False, out=proc.stdout or "", err=(proc.stderr or "").strip())
    return Ran(True, out=proc.stdout or "")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

CACHE_FILENAME = "fleet_cache.json"

# Five minutes. Long enough that re-running the digest twice while deciding
# what to do is free; short enough that a PR you just merged disappears from
# the next look. `--fresh` bypasses it when that is not good enough.
DEFAULT_TTL_SECONDS = 300


def cache_read(data_dir: Path, key: str, ttl: float = DEFAULT_TTL_SECONDS):
    """Return the cached value for *key*, or ``None`` if absent or stale."""
    if ttl <= 0:
        return None
    try:
        blob = json.loads((data_dir / CACHE_FILENAME).read_text(encoding="utf-8"))
        entry = blob[key]
        if (time.time() - float(entry["at"])) > ttl:
            return None
        return entry["value"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def cache_write(data_dir: Path, key: str, value) -> None:
    """Store *value* under *key*. A failure here is never fatal — it is a cache."""
    path = data_dir / CACHE_FILENAME
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(blob, dict):
            blob = {}
    except (OSError, json.JSONDecodeError, ValueError):
        blob = {}
    blob[key] = {"at": time.time(), "value": value}
    try:
        from lib.fsio import atomic_write_json

        atomic_write_json(path, blob)
    except (OSError, ImportError) as exc:
        logger.debug("fleet cache not written: %s", exc)
