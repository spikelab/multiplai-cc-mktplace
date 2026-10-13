"""The input files the session writes for the review page, and their checker.

The session writes these into the job's output directory; the review page
shows them and never edits them (skills/video-edit/schema/ has one JSON Schema
per file):

    job.json         {source, transcript, sentences, kind}
    candidates.json  [{id, start, end, speaker, hook, excerpt, scores, why}]
    looks.json       {styles, brands, captions}
    music.json       {beds, sfx_sets, voice_sample}

`check()` is a small standard-library checker for the JSON Schema keywords
those four files use, and nothing more: a keyword it does not know raises, so
a schema edit that needs more fails a test instead of being ignored. The
`absolute-path` format is this skill's own: an absolute path to a file that
exists.

Standard library only, like review_server.py, which imports this.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schema"
INPUTS = {"job": "job.json", "candidates": "candidates.json", "looks": "looks.json", "music": "music.json"}
MAX_ERRORS = 5

_ANNOTATIONS = {"$schema", "$id", "$defs", "title", "description"}
_KNOWN = _ANNOTATIONS | {"type", "properties", "required", "additionalProperties", "items", "enum",
                         "minimum", "maximum", "minLength", "maxLength", "minItems", "format", "$ref"}
_schemas: dict[str, dict] = {}


def schema(name: str) -> dict:
    if name not in _schemas:
        _schemas[name] = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
    return _schemas[name]


def _type_ok(value: Any, kind: str) -> bool:
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":     # bool is an int subclass in Python, never in JSON
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    raise ValueError(f"checker does not support type {kind!r}")


def check(value: Any, node: dict, root: dict | None = None, where: str = "$",
          errors: list[str] | None = None, paths: list[str] | None = None) -> list[str]:
    """Errors for `value` against schema `node`, as `<json path>: <problem>` lines.

    Strings checked by `format: absolute-path` are appended to `paths`.
    """
    root = root if root is not None else node
    errors = errors if errors is not None else []
    unknown = set(node) - _KNOWN
    if unknown:
        raise ValueError(f"checker does not support schema keyword(s) {sorted(unknown)}")
    if "$ref" in node:
        ref = node["$ref"]
        if not ref.startswith("#/$defs/"):
            raise ValueError(f"checker supports only local $defs references, not {ref!r}")
        return check(value, root["$defs"][ref[len("#/$defs/"):]], root, where, errors, paths)
    if "type" in node and not _type_ok(value, node["type"]):
        errors.append(f"{where}: expected {node['type']}")
        return errors
    if "enum" in node and value not in node["enum"]:
        errors.append(f"{where}: must be one of {', '.join(json.dumps(v) for v in node['enum'])}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in node and value < node["minimum"]:
            errors.append(f"{where}: must be >= {node['minimum']}")
        if "maximum" in node and value > node["maximum"]:
            errors.append(f"{where}: must be <= {node['maximum']}")
    if isinstance(value, str):
        if "minLength" in node and len(value) < node["minLength"]:
            errors.append(f"{where}: must not be empty" if node["minLength"] == 1
                          else f"{where}: shorter than {node['minLength']} characters")
        if "maxLength" in node and len(value) > node["maxLength"]:
            errors.append(f"{where}: longer than {node['maxLength']} characters")
        if node.get("format") == "absolute-path":
            if not Path(value).is_absolute():
                errors.append(f"{where}: must be an absolute path")
            elif paths is not None:
                paths.append(value)
        elif "format" in node:
            raise ValueError(f"checker does not support format {node['format']!r}")
    if isinstance(value, list):
        if "minItems" in node and len(value) < node["minItems"]:
            errors.append(f"{where}: needs at least {node['minItems']} item(s)")
        if "items" in node:
            for i, item in enumerate(value):
                check(item, node["items"], root, f"{where}[{i}]", errors, paths)
    if isinstance(value, dict):
        props = node.get("properties", {})
        for key in node.get("required", []):
            if key not in value:
                errors.append(f"{where}: missing {key!r}")
        for key, item in value.items():
            if key in props:
                check(item, props[key], root, f"{where}.{key}", errors, paths)
            elif node.get("additionalProperties") is False:
                errors.append(f"{where}: unknown field {key!r}")
    return errors


def _semantic(name: str, data: Any) -> list[str]:
    """What the schema cannot say: unique ids and names, end after start."""
    errors: list[str] = []
    if name == "candidates":
        seen: set[str] = set()
        for i, c in enumerate(data):
            if c["id"] in seen:
                errors.append(f"$[{i}].id: {c['id']!r} is used twice")
            seen.add(c["id"])
            if c["end"] <= c["start"]:
                errors.append(f"$[{i}]: end must be after start")
    elif name in ("looks", "music"):
        for key, items in data.items():
            if not isinstance(items, list):
                continue
            names = [x["name"] for x in items]
            for n in sorted({n for n in names if names.count(n) > 1}):
                errors.append(f"$.{key}: name {n!r} is used twice")
        vs = data.get("voice_sample")
        if vs and vs["end"] <= vs["start"]:
            errors.append("$.voice_sample: end must be after start")
    return errors


def validate(name: str, data: Any) -> tuple[list[str], list[str]]:
    """(errors, absolute paths named) for the parsed contents of input file `name`."""
    paths: list[str] = []
    errors = check(data, schema(name), paths=paths)
    if not errors:
        errors = _semantic(name, data)
    if not errors:
        errors = [f"file not found: {p}" for p in paths if not Path(p).is_file()]
    return errors, paths


def load(outdir: Path, name: str) -> dict:
    """{"status": "missing" | "invalid" | "ok", "file", and "error" or "data"}.

    "invalid" carries the first few problems, one per line, for the page to show.
    """
    path = outdir / INPUTS[name]
    out: dict[str, Any] = {"file": str(path)}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {**out, "status": "missing"}
    except (OSError, UnicodeDecodeError) as e:
        return {**out, "status": "invalid", "error": f"cannot read {path.name}: {e}"}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return {**out, "status": "invalid", "error": f"{path.name} is not valid JSON: {e}"}
    errors, _paths = validate(name, data)
    if errors:
        more = len(errors) - MAX_ERRORS
        lines = errors[:MAX_ERRORS] + ([f"… and {more} more"] if more > 0 else [])
        return {**out, "status": "invalid", "error": "\n".join(lines)}
    return {**out, "status": "ok", "data": data}


def named_files(outdir: Path) -> dict[str, set[Path]]:
    """Files the validated input files name, resolved, by what the page may fetch.

    source: the recording job.json names. audio: music beds and sound effects.
    sample: overlay sample pages. A file that fails validation names nothing.
    """
    out: dict[str, set[Path]] = {"source": set(), "audio": set(), "sample": set()}
    job = load(outdir, "job")
    if job["status"] == "ok":
        out["source"].add(Path(job["data"]["source"]).resolve())
    music = load(outdir, "music")
    if music["status"] == "ok":
        for bed in music["data"].get("beds", []):
            out["audio"].add(Path(bed["file"]).resolve())
        for s in music["data"].get("sfx_sets", []):
            out["audio"].update(Path(f).resolve() for f in s["files"])
    looks = load(outdir, "looks")
    if looks["status"] == "ok":
        out["sample"].update(Path(s["sample_html"]).resolve() for s in looks["data"].get("styles", []))
    return out
