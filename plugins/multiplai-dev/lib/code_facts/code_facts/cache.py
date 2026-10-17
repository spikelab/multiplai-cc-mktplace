"""The answer cache of `map_paths`: content keys, and a mapping that keeps its answers on disk.

`ask` and `run_tasks` store answers in whatever mapping they are given and
skip keys that already have one. Given a `_DiskAnswers` over a directory, every
stored answer is also written there as `<key>.json`, so a rerun on the same
directory asks nothing it already has. Answers are stored as plain JSON
values; callers re-validate them into their answer model when they read them.

A recoverable call failure is stored as `{"error": ...}`. `purge_failed`
removes those at the start of a run: a failed call is asked again on the next
run, never twice within one.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator, Mapping, MutableMapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from .explore import canonical_digest
from .paths import write_atomic

log = logging.getLogger(__name__)

# The key `ask` stores a recoverable call failure under.
CALL_ERROR = "error"

# A key is a sha256 hex digest, so it is also a safe file name.
KEY = re.compile(r"^[0-9a-f]{64}$")

CacheKind = Literal["fallback", "trace", "reask"]


def cache_key(kind: CacheKind, payload: Mapping[str, Any]) -> str:
    """The content key of one model request: the same whatever the key order of `payload`."""
    return canonical_digest({"kind": kind, "payload": dict(payload)})


class _DiskAnswers(MutableMapping[str, Any]):
    """Answers by key, each also kept as `<directory>/<key>.json`; in memory only when `directory` is None.

    Every answer in the directory is loaded at construction. A write goes to a
    temp file beside its target, fsync'd, then renamed over it, so a reader or
    a rerun never sees a partly written answer.
    """

    def __init__(self, directory: Path | None) -> None:
        self._directory = directory
        self._answers: dict[str, Any] = {}
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
            self._answers = _load(directory)

    def __getitem__(self, key: str) -> Any:
        return self._answers[key]

    def __setitem__(self, key: str, value: Any) -> None:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        if self._directory is not None:
            write_atomic(self._file(key), _encode(value))
        self._answers[key] = value

    def __delitem__(self, key: str) -> None:
        if key not in self._answers:
            raise KeyError(key)
        if self._directory is not None:
            self._file(key).unlink(missing_ok=True)
        del self._answers[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._answers)

    def __len__(self) -> int:
        return len(self._answers)

    def _file(self, key: str) -> Path:
        if not KEY.fullmatch(key):
            raise ValueError(f"answer key is not a sha256 hex digest: {key!r}")
        return self._directory / f"{key}.json"


def purge_failed(answers: _DiskAnswers) -> None:
    """Delete every stored call failure, so the next ask of its key goes out again."""
    for key in [k for k, v in answers.items() if isinstance(v, Mapping) and CALL_ERROR in v]:
        del answers[key]


def _encode(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _load(directory: Path) -> dict[str, Any]:
    """Every readable answer file in `directory`, by key. An unreadable one is a miss, asked again."""
    answers: dict[str, Any] = {}
    for path in sorted(directory.glob("*.json")):
        if not KEY.fullmatch(path.stem):
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            log.warning("skipping unreadable cached answer %s: %s", path, e)
            continue
        if not isinstance(value, dict):
            log.warning("skipping cached answer %s: not a JSON object", path)
            continue
        answers[path.stem] = value
    return answers
