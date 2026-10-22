"""Atomic publication of complete text files."""

import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, content: str) -> None:
    """Publish a complete document; interruption leaves the previous file intact."""
    if not content.strip():
        raise ValueError(f"Empty planning document: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".plan-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
