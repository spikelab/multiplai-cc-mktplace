# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core @ git+https://github.com/spikelab/multiplai-core@v0.8.1"]
# ///
"""Incremental qmd index refresh — detached child of the SessionStart hook.

Keeps the resources qmd index in sync with the resources directory:
``qmd update`` (re-index new/changed/removed files) followed by ``qmd
embed`` retry passes (embedding can die mid-run with "LLM session
expired" but is incremental, so retry while it still makes progress).

Safe to run often:

- flock guard — a second refresh for the same workspace exits immediately;
- everything is incremental — a no-op refresh costs one status call.

Index maintenance is CLI-only — it is deliberately NOT exposed over the
``http`` daemon's endpoint (that surface is read-only). So maintenance
reaches the host over the SSH bridge for both ``ssh`` and ``http`` modes;
in ``local`` mode it runs qmd on PATH. Under ``http`` this is best-effort:
if the bridge isn't deployed the refresh simply skips (fail-open) and
keeping the index fresh becomes the host's job (e.g. a launchd timer
running ``qmd update`` beside the daemon).

Usage: qmd_refresh.py <workspace-root>   (spawned by session_start.py)
"""

import fcntl
import hashlib
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.log_utils import setup_logging
from generators.config import load_catalog_config
from qmd_retrieval import QmdTarget, target_from_config

logger = setup_logging("qmd_refresh")

TIMEOUT_STATUS = 30
TIMEOUT_UPDATE = 600
TIMEOUT_EMBED = 1800
MAX_EMBED_PASSES = 5

_PENDING_RE = re.compile(r"(\d+) need embedding")


def run_qmd_raw(args: list[str], timeout: int, target: QmdTarget) -> str | None:
    """Run a raw qmd maintenance command; return stdout or None on failure.

    Same process-group kill discipline as qmd_retrieval.run_qmd — the qmd
    bin wrapper spawns a node grandchild that outlives a plain child kill.
    """
    # http mode queries over HTTP, but index maintenance isn't on that
    # endpoint — reach the host over the SSH bridge, same as ssh mode.
    if target.mode in ("ssh", "http"):
        remote = f"cd {target.workspace} && qmd {' '.join(args)}"
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                target.ssh_host, remote]
    else:
        import shutil
        qmd = shutil.which("qmd", path=f"{os.path.expanduser('~')}/.bun/bin:"
                           + os.environ.get("PATH", ""))
        if not qmd:
            return None
        argv = [qmd, *args]
    try:
        proc = subprocess.Popen(
            argv, cwd=target.workspace or None, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
    except OSError as e:
        logger.warning("qmd %s spawn failed: %s", args[0], type(e).__name__)
        return None
    try:
        stdout, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
        logger.warning("qmd %s timeout after %ds (group killed)", args[0], timeout)
        return None
    if proc.returncode != 0:
        logger.warning("qmd %s rc=%d: %s", args[0], proc.returncode, (stdout or "")[:300])
        return None
    return stdout or ""


def pending_embeddings(target: QmdTarget) -> int | None:
    """Documents still needing embedding, or None when status is unreachable."""
    out = run_qmd_raw(["status"], TIMEOUT_STATUS, target)
    if out is None:
        return None
    m = _PENDING_RE.search(out)
    return int(m.group(1)) if m else 0


def refresh(target: QmdTarget) -> None:
    if pending_embeddings(target) is None:
        logger.info("qmd unreachable (mode=%s) — skipping refresh", target.mode)
        return
    logger.info("refresh start (mode=%s workspace=%s)", target.mode, target.workspace)
    run_qmd_raw(["update"], TIMEOUT_UPDATE, target)
    prev = -1
    for i in range(1, MAX_EMBED_PASSES + 1):
        left = pending_embeddings(target)
        if left is None or left == 0:
            break
        if left == prev:
            logger.warning("embed stalled at %d pending", left)
            break
        prev = left
        logger.info("embed pass %d (%d pending)", i, left)
        run_qmd_raw(["embed"], TIMEOUT_EMBED, target)
    logger.info("refresh done (%s pending)", pending_embeddings(target))


def main() -> None:
    workspace = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("WORKSPACE", "")
    cfg = load_catalog_config()
    if not workspace:
        rd = cfg.resources_dir.strip()
        workspace = str(Path(rd).expanduser().parent) if rd else ""
    if not workspace or not cfg.enable_resources or cfg.resources_retrieval != "qmd":
        return

    # One refresh per workspace at a time; losers exit silently.
    key = hashlib.sha256(workspace.encode()).hexdigest()[:12]
    lock_path = f"/tmp/qmd-refresh-{key}.lock"
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return

    refresh(target_from_config(cfg, workspace))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            logger.exception("qmd refresh failed (non-fatal)")
        except Exception:
            pass
        sys.exit(0)
