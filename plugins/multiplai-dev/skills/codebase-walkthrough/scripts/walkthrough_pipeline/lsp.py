"""`pyright-langserver` references for the target's public Python symbols.

Catches re-exports and aliased imports inside the target's own repository
that `ast` misses. It runs over the target repo's snapshot, never the working
tree, behind one overall time limit. A timeout keeps whatever answers arrived
and is recorded, so the output can say LSP was cut short.

A minimal JSON-RPC client over stdio: `initialize`, `initialized`, one
`textDocument/didOpen` per queried file, then `textDocument/references` for
every symbol, all in flight at once. Server-to-client requests
(`workspace/configuration`, progress, registration) get an empty answer so
the server never waits on us.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from .models import Reference, SymbolEntry, TargetInfo

log = logging.getLogger(__name__)

SERVER = "pyright-langserver"


class _Client:
    def __init__(self, proc: asyncio.subprocess.Process):
        self.proc = proc
        self.next_id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        stdout = self.proc.stdout
        while True:
            headers = {}
            while True:
                line = await stdout.readline()
                if not line:
                    for fut in self.pending.values():
                        if not fut.done():
                            fut.set_exception(ConnectionError("language server exited"))
                    return
                line = line.decode("ascii", errors="replace").strip()
                if not line:
                    break
                k, _, v = line.partition(":")
                headers[k.lower()] = v.strip()
            body = await stdout.readexactly(int(headers.get("content-length", "0")))
            msg = json.loads(body)
            if "id" in msg and "method" in msg:      # a request from the server
                await self._send({"jsonrpc": "2.0", "id": msg["id"],
                                  "result": [{}] * len(msg.get("params", {}).get("items", []))
                                  if msg["method"] == "workspace/configuration" else None})
            elif "id" in msg and msg["id"] in self.pending:
                fut = self.pending.pop(msg["id"])
                if not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError(str(msg["error"])[:300]))
                    else:
                        fut.set_result(msg.get("result"))

    async def _send(self, msg: dict) -> None:
        data = json.dumps(msg).encode("utf-8")
        self.proc.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data)
        await self.proc.stdin.drain()

    async def request(self, method: str, params: dict):
        self.next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = fut
        await self._send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        return await fut

    async def notify(self, method: str, params: dict) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})


def _uri_path(uri: str) -> Path:
    return Path(unquote(urlparse(uri).path))


async def find_references(target: TargetInfo, snapshot: Path, symbols: list[SymbolEntry], *,
                          timeout_s: float = 300.0) -> tuple[list[Reference], dict]:
    """(references outside the target subpath, a report: {status, seconds, symbols, answered})."""
    report = {"status": "skipped", "seconds": 0.0, "symbols": 0, "answered": 0}
    exe = shutil.which(SERVER)
    if timeout_s <= 0:
        report["status"] = "skipped (--lsp-timeout 0)"
        return [], report
    if exe is None:
        report["status"] = f"skipped ({SERVER} not installed)"
        return [], report
    wanted = [e for e in symbols if e.kind in ("function", "class") and not e.name.startswith("_")]
    report["symbols"] = len(wanted)
    if not wanted:
        report["status"] = "no public symbols"
        return [], report

    config = snapshot / "pyrightconfig.json"
    config.write_text(json.dumps({"exclude": ["**/migrations", "**/node_modules", "**/.venv", "**/static"],
                                  "typeCheckingMode": "off"}), encoding="utf-8")
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        exe, "--stdio", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, cwd=str(snapshot))
    client = _Client(proc)
    answers: dict[int, list] = {}
    prefix = target.repo_key + "/"

    async def run() -> None:
        root = snapshot.resolve().as_uri()
        await client.request("initialize", {
            "processId": None, "rootUri": root, "workspaceFolders": [{"uri": root, "name": target.repo_key}],
            "capabilities": {"textDocument": {"references": {}}, "workspace": {"configuration": True}},
        })
        await client.notify("initialized", {})
        opened = set()
        for e in wanted:
            rel = e.path[len(prefix):]
            if rel not in opened:
                opened.add(rel)
                f = snapshot / rel
                await client.notify("textDocument/didOpen", {"textDocument": {
                    "uri": f.resolve().as_uri(), "languageId": "python", "version": 1,
                    "text": f.read_text(encoding="utf-8", errors="replace")}})

        async def one(i: int, e: SymbolEntry) -> None:
            rel = e.path[len(prefix):]
            lines = (snapshot / rel).read_text(encoding="utf-8", errors="replace").split("\n")
            col = lines[e.line - 1].find(e.name)
            if col < 0:
                return
            try:
                answers[i] = await client.request("textDocument/references", {
                    "textDocument": {"uri": (snapshot / rel).resolve().as_uri()},
                    "position": {"line": e.line - 1, "character": col},
                    "context": {"includeDeclaration": False}}) or []
            except (RuntimeError, ConnectionError) as err:
                log.debug("references for %s failed: %s", e.name, err)

        await asyncio.gather(*(one(i, e) for i, e in enumerate(wanted)))

    try:
        await asyncio.wait_for(run(), timeout=timeout_s)
        report["status"] = "ok"
    except asyncio.TimeoutError:
        report["status"] = f"timed out after {timeout_s:g}s"
    except (ConnectionError, OSError, json.JSONDecodeError) as e:
        report["status"] = f"failed ({type(e).__name__}: {str(e)[:120]})"
    finally:
        report["seconds"] = round(time.monotonic() - start, 1)
        report["answered"] = len(answers)
        client.reader.cancel()
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        config.unlink(missing_ok=True)

    sub = target.subpath.rstrip("/") + "/" if target.subpath else ""
    snap_root = snapshot.resolve()
    out: list[Reference] = []
    seen: set[tuple] = set()
    text_cache: dict[str, list[str]] = {}
    for i, locs in answers.items():
        e = wanted[i]
        for loc in locs:
            try:
                rel = _uri_path(loc["uri"]).resolve().relative_to(snap_root).as_posix()
            except (ValueError, KeyError):
                continue
            if sub and rel.startswith(sub):
                continue
            line = loc["range"]["start"]["line"] + 1
            if (rel, line) in seen:
                continue
            seen.add((rel, line))
            if rel not in text_cache:
                text_cache[rel] = (snapshot / rel).read_text(encoding="utf-8", errors="replace").split("\n")
            lines = text_cache[rel]
            quote = lines[line - 1].strip() if 0 < line <= len(lines) else ""
            if quote:
                out.append(Reference(repo=target.repo_key, kind="python-ref", symbol=e.name, path=prefix + rel,
                                     line=line, quote=quote, method="lsp"))
    return out, report
