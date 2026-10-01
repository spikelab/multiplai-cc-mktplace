"""The code half of the docs stage: find, fetch and cache vendor docs; map the API collection.

This stage is network plus untrusted text, a different risk from the rest:

- Only `<docs>/llms.txt` and page URLs it lists, on the `--docs` host, are
  fetched, each through the SSRF guard. Nothing else goes on the network.
  No request is ever sent to the vendor's API.
- Every page is defanged and stored inside an `<untrusted-content>` fence.
  The docs agents get `Read` on the cache only.
- The API collection is read field by field: `info.name`, `http.method`,
  `http.url` and header *names*. `.env*` files are never opened. Environment
  files are read for the `server` variable's value and nothing else, and never
  when that variable is marked secret.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import yaml

from . import fetcher
from .symbols import normalise_api_path
from .target import is_secret_path
from .untrusted import defang_untrusted, is_fetchable_url

log = logging.getLogger(__name__)

LLMS_LINK = re.compile(r"^\s*[-*]\s*\[([^\]]+)\]\((\S+?)\)(?::\s*(.*))?$", re.M)
DOC_SUFFIXES = (".md", ".mdx", ".txt", ".rst")
COLLECTION_MARKERS = ("opencollection.yml", "bruno.json")


class DocsError(Exception):
    """llms.txt could not be used. The message says why."""


def vendor_word(docs_url: str) -> str:
    """`https://docs.channex.io` -> `channex`."""
    host = urlsplit(docs_url).hostname or ""
    labels = [x for x in host.split(".") if x not in ("www", "docs", "api", "developer", "developers")]
    return labels[0].lower() if labels else host


def find_local_docs(search_root: Path, vendor: str) -> list[Path]:
    """Directories directly under *search_root* whose name holds the vendor word and that are not repos
    or API collections."""
    out = []
    if not vendor or not search_root.is_dir():
        return out
    for d in sorted(search_root.iterdir()):
        if (d.is_dir() and vendor in d.name.lower() and not (d / ".git").exists()
                and not any((d / m).exists() for m in COLLECTION_MARKERS)):
            out.append(d)
    return out


def find_collection(search_root: Path, vendor: str) -> Path | None:
    if not search_root.is_dir():
        return None
    for d in sorted(search_root.iterdir()):
        if d.is_dir() and any((d / m).exists() for m in COLLECTION_MARKERS) and (not vendor or vendor in d.name.lower()):
            return d
    return None


def read_local_docs(dirs: list[Path]) -> dict[str, str]:
    """{`<dir name>/<file>`: text} for doc files under each directory."""
    out: dict[str, str] = {}
    for d in dirs:
        for f in sorted(d.rglob("*")):
            if f.is_file() and f.suffix.lower() in DOC_SUFFIXES and not is_secret_path(f.name):
                out[f"{d.name}/{f.relative_to(d).as_posix()}"] = f.read_text(encoding="utf-8", errors="replace")
    return out


def parse_llms(text: str, docs_url: str) -> list[tuple[str, str, str]]:
    """(title, url, description) for every link in llms.txt on the docs host."""
    host = urlsplit(docs_url).hostname
    out = []
    for m in LLMS_LINK.finditer(text):
        title, url, desc = m.group(1).strip(), m.group(2).strip(), (m.group(3) or "").strip()
        if is_fetchable_url(url) and urlsplit(url).hostname == host:
            out.append((title, url, desc))
    return out


def resources(api_paths: list[str]) -> list[str]:
    """First path segment of each outbound API path, in order: `/booking_revisions/{}/ack` -> `booking_revisions`."""
    seen: dict[str, None] = {}
    for p in api_paths:
        seg = next((s for s in p.split("/") if s and s != "{}"), "")
        if seg:
            seen.setdefault(seg, None)
    return list(seen)


def _phrases(resource: str) -> set[str]:
    base = resource.lower().replace("_", " ").replace("-", " ")
    words = base.split()
    singular = " ".join(words[:-1] + [words[-1][:-1]]) if words and words[-1].endswith("s") else base
    return {base, singular}


def _norm(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", text.lower()) + " "


GENERIC_WORDS = {"options", "option", "list", "feed", "items", "values", "data", "info", "info"}


def _words(resource: str) -> set[str]:
    out = set()
    for w in resource.lower().replace("-", "_").split("_"):
        w = w[:-1] if w.endswith("s") else w
        if len(w) >= 4 and w not in GENERIC_WORDS:
            out.add(w)
    return out


def choose_pages(links: list[tuple[str, str, str]], api_paths: list[str], cap: int) -> dict[str, list[str]]:
    """{resource: [url, ...]}: a page is chosen when its title or path shares a resource word.

    The whole resource name is tried first (`rate plans`); a resource no page
    names whole falls back to its single words (`booking`, `revision`).
    Resources in the order the code calls them, pages in llms.txt order, at
    most *cap* in all.
    """
    chosen: dict[str, list[str]] = {}
    taken: set[str] = set()

    def matches(title: str, url: str, needles: set[str]) -> bool:
        hay = _norm(title) + _norm(urlsplit(url).path)
        return any(f" {n} " in hay or f" {n}s " in hay for n in needles)

    for res in resources(api_paths):
        hits = [url for title, url, _ in links if matches(title, url, _phrases(res))]
        if not hits:
            hits = [url for title, url, _ in links if matches(title, url, _words(res))]
        for url in hits:
            chosen.setdefault(res, [])
            if url not in chosen[res] and (url in taken or len(taken) < cap):
                chosen[res].append(url)
                taken.add(url)
    return chosen


def fence(source: str, text: str) -> str:
    return f'<untrusted-content source="{defang_untrusted(source)}">\n{defang_untrusted(text)}\n</untrusted-content>\n'


def cache_name(url: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9]+", "-", PurePosixPath(urlsplit(url).path).stem or "page").strip("-")[:60]
    return f"{stem}-{hashlib.sha1(url.encode()).hexdigest()[:8]}.md"


async def fetch_docs(docs_url: str, pages_wanted: list[str], cache: Path) -> tuple[dict[str, str], dict[str, str], dict]:
    """(defanged text by url, cache file by url, report). Raises DocsError when llms.txt is unusable."""
    cache.mkdir(parents=True, exist_ok=True)
    texts: dict[str, str] = {}
    files: dict[str, str] = {}
    report: dict = {"fetched": [], "failed": {}}
    host = urlsplit(docs_url).hostname
    async with fetcher.client() as client:
        results = await asyncio.gather(*(fetcher.fetch_url(u, client, allowed_host=host) for u in pages_wanted))
    for r in results:
        if not r.ok:
            report["failed"][r.url] = r.error or f"HTTP {r.status}"
            continue
        clean = defang_untrusted(r.text)
        name = cache_name(r.url)
        (cache / name).write_text(fence(r.url, r.text), encoding="utf-8")
        texts[r.url] = clean
        files[r.url] = name
        report["fetched"].append(r.url)
    return texts, files, report


async def fetch_llms(docs_url: str) -> tuple[str, list[tuple[str, str, str]]]:
    """(raw text, links). Raises DocsError when it does not return 200 or lists no `.md` pages."""
    url = docs_url.rstrip("/") + "/llms.txt"
    if not is_fetchable_url(url):
        raise DocsError(f"{url} is not a fetchable URL")
    async with fetcher.client() as client:
        r = await fetcher.fetch_url(url, client, allowed_host=urlsplit(docs_url).hostname)
    if r.status != 200:
        raise DocsError(f"{url} returned {r.status or r.error}, not 200")
    links = parse_llms(r.text, docs_url)
    md = [l for l in links if urlsplit(l[1]).path.endswith(".md")]
    if not md:
        raise DocsError(f"{url} lists no page URLs ending in .md")
    return r.text, md


# --- API collection -------------------------------------------------------------------


def read_collection(root: Path) -> tuple[list[dict], str]:
    """(requests, server) from a Bruno OpenCollection. Each request: {file, name, method, url, path, headers}.

    Never opens `.env*`. Environment files give only the `server` value, and
    only when it is not marked secret.
    """
    requests: list[dict] = []
    server = ""
    for f in sorted(root.rglob("*")):
        rel = f.relative_to(root).as_posix()
        if not f.is_file() or is_secret_path(f.name) or f.suffix.lower() not in (".yml", ".yaml"):
            continue
        if f.name in COLLECTION_MARKERS:
            continue
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8", errors="replace"))
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        if rel.startswith("environments/"):
            if not server:
                for var in data.get("variables") or []:
                    if isinstance(var, dict) and var.get("name") == "server" and not var.get("secret"):
                        server = str(var.get("value") or "")
            continue
        http = data.get("http")
        if not isinstance(http, dict) or not http.get("url"):
            continue
        url = str(http["url"])
        path = re.sub(r"^\{\{server\}\}", "", url)
        path = re.sub(r"^https?://[^/]+", "", path)
        path = re.sub(r"^/api/(\{\{version\}\}|v\d+)", "", path)
        headers = [str(h.get("name")) for h in http.get("headers") or [] if isinstance(h, dict) and h.get("name")]
        requests.append({"file": f"{root.name}/{rel}", "name": str((data.get("info") or {}).get("name") or ""),
                         "method": str(http.get("method") or ""), "url": url,
                         "path": normalise_api_path(path), "headers": headers})
    return requests, server


def map_collection(api_paths: list[str], requests: list[dict]) -> tuple[dict[str, list[dict]], list[str]]:
    """({api path: [requests]}, [api paths not in the collection]). `{x}` and `{{x}}` are the same placeholder."""
    mapped: dict[str, list[dict]] = {}
    missing: list[str] = []
    for p in api_paths:
        hits = [r for r in requests if r["path"] == normalise_api_path(p)]
        if hits:
            mapped[p] = hits
        else:
            missing.append(p)
    return mapped, missing
