# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core @ git+https://github.com/spikelab/multiplai-core@v0.5.1"]
# ///
"""Log digest for the multiplai runtime logs (the /log-doctor skill).

Scans ``paths.logs_dir()``, parses every log file, clusters ERROR/WARNING/INFO
entries by normalized signature, and emits a markdown (or JSON) digest with
per-subsystem stats, cross-cutting health anomalies, and traceback tails.
Read-only — never modifies logs. Supports focusing on one or more subsystems
(``--subsystem``), a recency window (``--days``), and severity filtering
(``--errors-only``).

Understands the three formats present in the logs directory:

1. Standard lines (see reference/dev/logging-standard.md):
   ``[2026-07-06T07:36:08Z] [component] [session:xxxxxxxx] LEVEL: message``
   Continuation lines (tracebacks, wrapped output) attach to the entry above.
2. Activity feed short lines (``activity*.log``):
   ``07:36:08Z [5159085d] [context] message``
3. Activity feed JSONL (``activity*.jsonl``).
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.log_utils import setup_logging
from multiplai_core.paths import get_paths

logger = setup_logging("log_doctor")

# filename → subsystem: "<name>-YYYY-MM-DD.log" or "<name>.log" / ".jsonl"
FILENAME_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_-]+?)(?:-(?P<date>\d{4}-\d{2}-\d{2}))?\.(?P<ext>log|jsonl)$"
)

STANDARD_LINE_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z?\]\s+"
    r"\[(?P<component>[^\]]+)\]\s+"
    r"\[session:(?P<session>[^\]]*)\]\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL):\s?"
    r"(?P<msg>.*)$"
)

ACTIVITY_LINE_RE = re.compile(
    r"^(?P<time>\d{2}:\d{2}:\d{2})Z?\s+"
    r"\[(?P<session>[^\]]*)\]\s+"
    r"\[(?P<component>[^\]]+)\]\s+"
    r"(?P<msg>.*)$"
)

# Append-only logs the logging standard says get truncated around 100KB.
APPEND_ONLY_TRUNCATE_BYTES = 100 * 1024

SEVERITY_ORDER = {"CRITICAL": 0, "ERROR": 1, "WARNING": 2, "INFO": 3, "DEBUG": 4}

NORMALIZE_PATTERNS = [
    (re.compile(r"'[^']*'"), "'…'"),
    (re.compile(r'"[^"]*"'), '"…"'),
    (re.compile(r"/[\w./~+-]{2,}"), "<path>"),
    (re.compile(r"\b[0-9a-f]{8,}\b"), "<hex>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]?[\d:.Z]*\b"), "<ts>"),
    (re.compile(r"\b\d+(\.\d+)?\b"), "<n>"),
]


@dataclass
class Entry:
    subsystem: str
    file: str
    ts: datetime | None
    level: str
    session: str
    msg: str
    detail_lines: int = 0  # continuation lines (e.g. traceback depth)
    detail_tail: str = ""  # last continuation line (usually the exception)


@dataclass
class Cluster:
    signature: str
    level: str
    subsystem: str
    count: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    sample: Entry | None = None
    files: set = field(default_factory=set)


@dataclass
class FileStat:
    path: Path
    subsystem: str
    size: int
    entries: int = 0
    unparsed: int = 0
    levels: dict = field(default_factory=dict)


def normalize(msg: str) -> str:
    """Collapse variable parts of a message into a stable signature."""
    sig = msg.strip()
    for pat, repl in NORMALIZE_PATTERNS:
        sig = pat.sub(repl, sig)
    return sig[:200]


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.rstrip("Z"))
    except ValueError:
        return None


def discover(logs_dir: Path, subsystems: list | None = None) -> dict:
    """Map subsystem name → its log files (current + date-rotated)."""
    found: dict[str, list[Path]] = {}
    for path in sorted(logs_dir.iterdir()):
        m = FILENAME_RE.match(path.name)
        if not m or not path.is_file():
            continue
        name = m.group("name")
        if subsystems and name not in subsystems:
            continue
        found.setdefault(name, []).append(path)
    return found


def parse_file(path: Path, subsystem: str, file_date: date | None):
    stat = FileStat(path=path, subsystem=subsystem, size=path.stat().st_size)
    entries: list[Entry] = []
    is_jsonl = path.suffix == ".jsonl"
    try:
        text = path.read_text(errors="replace")
    except OSError as err:
        logger.warning("SKIP file=%s reason=%s", path, err)
        return entries, stat

    for line in text.splitlines():
        if not line.strip():
            continue
        entry = None
        if is_jsonl:
            entry = _parse_jsonl_line(line, subsystem, path.name)
        else:
            m = STANDARD_LINE_RE.match(line)
            if m:
                entry = Entry(
                    subsystem=subsystem,
                    file=path.name,
                    ts=_parse_ts(m.group("ts")),
                    level=m.group("level"),
                    session=m.group("session") or "--------",
                    msg=m.group("msg"),
                )
            else:
                m = ACTIVITY_LINE_RE.match(line)
                if m:
                    ts = None
                    if file_date:
                        ts = _parse_ts(f"{file_date.isoformat()}T{m.group('time')}")
                    entry = Entry(
                        subsystem=subsystem,
                        file=path.name,
                        ts=ts,
                        level="INFO",
                        session=m.group("session"),
                        msg=f"[{m.group('component')}] {m.group('msg')}",
                    )
        if entry is not None:
            entries.append(entry)
            stat.entries += 1
            stat.levels[entry.level] = stat.levels.get(entry.level, 0) + 1
        elif is_jsonl:
            # JSONL has no continuation lines — a bad line is just unparsed
            stat.unparsed += 1
        elif entries:
            # continuation line (traceback etc.) — attach to previous entry
            entries[-1].detail_lines += 1
            if line.strip():
                entries[-1].detail_tail = line.strip()
        else:
            stat.unparsed += 1
    return entries, stat


def _parse_jsonl_line(line: str, subsystem: str, filename: str) -> Entry | None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return Entry(
        subsystem=subsystem,
        file=filename,
        ts=_parse_ts(str(obj.get("ts", ""))),
        level=str(obj.get("level", "INFO")),
        session=str(obj.get("session", "--------")),
        msg=str(obj.get("msg", ""))
        or f"{obj.get('component', '?')}:{obj.get('event', '?')}",
    )


def cluster(entries: list) -> list:
    """Group entries by (subsystem, level, normalized message); worst/most frequent first."""
    clusters: dict[tuple, Cluster] = {}
    for e in entries:
        key = (e.subsystem, e.level, normalize(e.msg))
        c = clusters.get(key)
        if c is None:
            c = clusters[key] = Cluster(
                signature=key[2], level=e.level, subsystem=e.subsystem
            )
        c.count += 1
        c.files.add(e.file)
        if e.ts:
            if c.first_seen is None or e.ts < c.first_seen:
                c.first_seen = e.ts
            if c.last_seen is None or e.ts > c.last_seen:
                c.last_seen = e.ts
        # prefer a sample that carries a traceback tail
        if c.sample is None or (e.detail_tail and not c.sample.detail_tail):
            c.sample = e
    return sorted(
        clusters.values(),
        key=lambda c: (SEVERITY_ORDER.get(c.level, 5), -c.count),
    )


def health_checks(stats: list, entries: list) -> list:
    """Cross-cutting anomalies the clusters themselves won't show."""
    notes: list[str] = []
    for s in stats:
        if s.path.name == "hook-errors.log" and s.size > APPEND_ONLY_TRUNCATE_BYTES:
            notes.append(
                f"{s.path.name} is {s.size // 1024}KB — logging standard says append-only "
                f"logs are truncated to ~100KB when oversized; truncation is not happening."
            )
        if s.size > 0 and s.entries == 0 and s.unparsed > 0:
            notes.append(
                f"{s.path.name}: {s.unparsed} lines, none parseable — format drift from "
                f"the logging standard."
            )
    parsed = [e for e in entries if e.level != "DEBUG"]
    if parsed:
        unknown = sum(1 for e in parsed if e.session.strip("-") == "")
        ratio = unknown / len(parsed)
        if ratio > 0.5:
            notes.append(
                f"{unknown}/{len(parsed)} entries ({ratio:.0%}) have no session id "
                f"([session:--------]) — session propagation is broken for most components."
            )
    return notes


def scan(
    logs_dir: Path,
    subsystems: list | None = None,
    since: date | None = None,
    errors_only: bool = False,
):
    files_by_subsystem = discover(logs_dir, subsystems)
    all_entries: list[Entry] = []
    stats: list[FileStat] = []
    for name, paths in files_by_subsystem.items():
        for path in paths:
            m = FILENAME_RE.match(path.name)
            file_date = (
                date.fromisoformat(m.group("date"))
                if m and m.group("date")
                else date.fromtimestamp(path.stat().st_mtime)
            )
            if since and file_date < since:
                continue
            entries, stat = parse_file(path, name, file_date)
            stats.append(stat)
            all_entries.extend(entries)
    if since:
        all_entries = [e for e in all_entries if e.ts is None or e.ts.date() >= since]
    if errors_only:
        all_entries = [
            e for e in all_entries if e.level in ("ERROR", "CRITICAL", "WARNING")
        ]
    notes = health_checks(stats, all_entries)
    return cluster(all_entries), stats, notes, files_by_subsystem


def render_markdown(clusters, stats, notes, max_clusters: int) -> str:
    out = ["# multiplai log digest", ""]

    out.append("## Subsystems scanned")
    out.append("")
    out.append("| Subsystem | Files | Entries | Errors | Warnings |")
    out.append("|---|---|---|---|---|")
    by_name: dict[str, list] = {}
    for s in stats:
        by_name.setdefault(s.subsystem, []).append(s)
    for name in sorted(by_name):
        group = by_name[name]
        entries = sum(s.entries for s in group)
        errors = sum(
            s.levels.get("ERROR", 0) + s.levels.get("CRITICAL", 0) for s in group
        )
        warnings = sum(s.levels.get("WARNING", 0) for s in group)
        out.append(f"| {name} | {len(group)} | {entries} | {errors} | {warnings} |")
    out.append("")

    if notes:
        out.append("## Health anomalies")
        out.append("")
        for n in notes:
            out.append(f"- {n}")
        out.append("")

    out.append(f"## Top clusters (by severity, then frequency; max {max_clusters})")
    out.append("")
    for c in clusters[:max_clusters]:
        span = ""
        if c.first_seen and c.last_seen:
            span = f" · {c.first_seen.date()} → {c.last_seen.date()}"
        out.append(f"### [{c.level}] {c.subsystem} ×{c.count}{span}")
        out.append("")
        out.append(f"- signature: `{c.signature}`")
        if c.sample:
            out.append(f"- sample: `{c.sample.msg[:300]}`")
            if c.sample.detail_tail:
                out.append(
                    f"- traceback tail ({c.sample.detail_lines} lines): "
                    f"`{c.sample.detail_tail[:300]}`"
                )
        out.append(f"- files: {', '.join(sorted(c.files))}")
        out.append("")
    return "\n".join(out)


def render_json(clusters, stats, notes, max_clusters: int) -> str:
    return json.dumps(
        {
            "subsystems": sorted({s.subsystem for s in stats}),
            "files": [
                {
                    "path": str(s.path),
                    "subsystem": s.subsystem,
                    "size": s.size,
                    "entries": s.entries,
                    "levels": s.levels,
                }
                for s in stats
            ],
            "health_anomalies": notes,
            "clusters": [
                {
                    "level": c.level,
                    "subsystem": c.subsystem,
                    "count": c.count,
                    "signature": c.signature,
                    "first_seen": c.first_seen.isoformat() if c.first_seen else None,
                    "last_seen": c.last_seen.isoformat() if c.last_seen else None,
                    "sample_msg": c.sample.msg if c.sample else None,
                    "traceback_tail": c.sample.detail_tail if c.sample else None,
                    "files": sorted(c.files),
                }
                for c in clusters[:max_clusters]
            ],
        },
        indent=2,
    )


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="log_doctor")
    parser.add_argument(
        "--logs-dir", help="logs directory (default: paths.logs_dir())"
    )
    parser.add_argument(
        "--subsystem",
        help="comma-separated subsystem names to focus on (default: all)",
    )
    parser.add_argument("--days", type=int, help="only scan the last N days")
    parser.add_argument(
        "--errors-only",
        action="store_true",
        help="only WARNING/ERROR/CRITICAL entries",
    )
    parser.add_argument("--max-clusters", type=int, default=25)
    parser.add_argument(
        "--json", action="store_true", help="emit JSON instead of markdown"
    )
    parser.add_argument(
        "--list", action="store_true", help="list available subsystems and exit"
    )
    args = parser.parse_args(argv)

    logs_dir = (
        Path(args.logs_dir).expanduser() if args.logs_dir else get_paths().logs_dir()
    )
    if not logs_dir.is_dir():
        print(f"logs directory not found: {logs_dir}", file=sys.stderr)
        return 2

    subsystems = (
        [s.strip() for s in args.subsystem.split(",") if s.strip()]
        if args.subsystem
        else None
    )
    since = date.today() - timedelta(days=args.days) if args.days else None

    logger.info(
        "START logs_dir=%s subsystems=%s since=%s errors_only=%s",
        logs_dir, subsystems or "all", since, args.errors_only,
    )
    clusters, stats, notes, found = scan(
        logs_dir, subsystems=subsystems, since=since, errors_only=args.errors_only
    )

    if args.list:
        for name in sorted(found):
            print(f"{name}  ({len(found[name])} files)")
        return 0

    if subsystems:
        missing = [s for s in subsystems if s not in found]
        if missing:
            print(
                f"warning: no logs for subsystem(s): {', '.join(missing)} "
                f"(use --list to see available)",
                file=sys.stderr,
            )

    if args.json:
        print(render_json(clusters, stats, notes, args.max_clusters))
    else:
        print(render_markdown(clusters, stats, notes, args.max_clusters))
    logger.info(
        "DONE files=%d entries=%d clusters=%d anomalies=%d",
        len(stats), sum(s.entries for s in stats), len(clusters), len(notes),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
