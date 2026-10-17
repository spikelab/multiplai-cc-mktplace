"""schema/paths.v1.schema.json: header, strictness, and the documents it accepts and rejects."""

from __future__ import annotations

import copy
import importlib.resources
import json
from pathlib import Path

import jsonschema
import pytest

from conftest import paths_name, unscoped_variant

FACTS_SCHEMA = Path(__file__).resolve().parents[3] / "skills/review/schema/facts.v1.schema.json"
TOP_LEVEL = ["schema_version", "commit", "scoped", "cap", "fallback", "paths", "dropped", "diagnostics",
             "test_listing_skips"]


def _schema() -> dict:
    ref = importlib.resources.files("code_facts").joinpath("schema", "paths.v1.schema.json")
    assert ref.is_file(), "code_facts/schema/paths.v1.schema.json is missing"
    return json.loads(ref.read_text(encoding="utf-8"))


def _validator():
    schema = _schema()
    return jsonschema.validators.validator_for(schema)(schema)


def _errors(doc: dict) -> list:
    return list(_validator().iter_errors(doc))


def _deref(schema: dict, node: dict) -> dict:
    """Follow #/$defs refs, and step into the non-null arm of a nullable anyOf."""
    while True:
        if "$ref" in node:
            name = node["$ref"].removeprefix("#/$defs/")
            node = schema["$defs"][name]
        elif "anyOf" in node:
            node = next(arm for arm in node["anyOf"] if arm.get("type") != "null")
        elif node.get("type") == "array" and "items" in node:
            node = node["items"]
        else:
            return node


def _props(schema: dict, *path: str) -> list[str]:
    node = schema
    for key in path:
        node = _deref(schema, node["properties"][key])
    return list(_deref(schema, node)["properties"])


def _set(doc: dict, path: tuple, value) -> dict:
    doc = copy.deepcopy(doc)
    node = doc
    for key in path[:-1]:
        node = node[key]
    if value is _DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return doc


_DELETE = object()


def test_header_follows_facts_v1():
    facts = json.loads(FACTS_SCHEMA.read_text())
    schema = _schema()
    assert schema["$schema"] == facts["$schema"]
    assert schema["$id"] == facts["$id"].replace("facts.v1", "paths.v1")
    assert schema["title"]
    assert schema["$defs"]


def test_schema_is_valid_under_its_meta_schema():
    schema = _schema()
    jsonschema.validators.validator_for(schema).check_schema(schema)


def test_every_object_forbids_additional_properties():
    offenders = []

    def walk(node, where):
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                if node.get("additionalProperties") is not False:
                    offenders.append(where)
            for k, v in node.items():
                walk(v, f"{where}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{where}/{i}")

    walk(_schema(), "#")
    assert offenders == []


def test_property_order_matches_the_normative_field_order():
    schema = _schema()
    assert list(schema["properties"]) == TOP_LEVEL
    assert set(schema["required"]) == set(TOP_LEVEL)
    assert _props(schema, "fallback") == ["status", "reason"]
    assert _props(schema, "paths") == ["id", "entry", "hops", "stops_at", "break_reason",
                                       "changed_lines_covered", "tests"]
    assert _props(schema, "paths", "entry") == ["kind", "name", "location", "handler", "invocation",
                                                "also_invoked_by"]
    assert _props(schema, "paths", "entry", "location") == ["path", "line"]
    assert _props(schema, "paths", "entry", "handler") == ["path", "line", "symbol"]
    assert _props(schema, "paths", "entry", "also_invoked_by") == ["kind", "name", "location"]
    assert _props(schema, "paths", "tests", "e2e") == ["node_id", "file", "line", "match", "invocation_line"]
    assert _props(schema, "paths", "tests", "mocks_hop_2") == ["node_id", "file", "line", "match",
                                                               "invocation_line", "patch_file", "patch_line"]
    assert _props(schema, "dropped") == ["stage", "reason", "detail", "entry"]
    assert _props(schema, "diagnostics") == ["path", "line", "reason", "detail"]
    assert _props(schema, "test_listing_skips") == ["file", "reason"]


def test_hop_properties_are_the_model_fields():
    schema = _schema()
    assert _props(schema, "paths", "hops") == list(paths_name("PathHop").model_fields)


def test_full_and_unscoped_documents_validate_and_parse(full_paths_doc):
    for doc in (full_paths_doc, unscoped_variant(full_paths_doc)):
        assert _errors(doc) == []
        paths_name("PathsResult").model_validate(doc)


@pytest.mark.parametrize("status,reason", [("ok", None), ("skipped", None), ("failed", "bad answer")])
def test_every_fallback_status_validates(full_paths_doc, status, reason):
    doc = _set(full_paths_doc, ("fallback",), {"status": status, "reason": reason})
    assert _errors(doc) == []


@pytest.mark.parametrize("path,value", [
    (("schema_version",), "paths.v2"),
    (("dropped",), _DELETE),
    (("test_listing_skips",), _DELETE),
    (("foo",), 1),
    (("paths", 0, "foo"), 1),
    (("paths", 0, "entry", "foo"), 1),
    (("paths", 0, "hops", 0, "foo"), 1),
    (("dropped", 0, "foo"), 1),
    (("paths", 0, "hops"), []),
    (("paths", 0, "id"), "p0"),
    (("paths", 0, "id"), "path1"),
    (("paths", 0, "id"), "p01"),
    (("paths", 0, "entry", "kind"), "route"),
    (("paths", 0, "entry", "location", "path"), "/abs/x.py"),
    (("paths", 0, "entry", "location", "path"), "../x.py"),
    (("paths", 0, "entry", "location", "path"), "a/../b.py"),
    (("paths", 0, "entry", "location", "path"), "./x.py"),
    (("paths", 0, "entry", "location", "path"), "a\\b.py"),
    (("paths", 0, "entry", "location", "path"), ""),
    (("paths", 0, "entry", "location", "line"), 0),
    (("paths", 0, "changed_lines_covered", 0, "line"), 0),
    (("dropped", 0, "reason"), ""),
    (("dropped", 0, "stage"), "explore"),
    (("diagnostics", 0, "reason"), "parse_errors"),
    (("test_listing_skips", 0, "reason"), "io_error"),
    (("fallback", "status"), "done"),
    (("paths", 0, "tests", "e2e", 0, "match"), "magic"),
    (("paths", 0, "hops", 0, "side"), "base"),
    (("scoped",), "false"),
])
def test_schema_rejects(full_paths_doc, path, value):
    assert _errors(_set(full_paths_doc, path, value)), f"{path}={value!r} was accepted"


def test_more_than_twenty_hops_is_rejected(full_paths_doc):
    hop = full_paths_doc["paths"][1]["hops"][0]
    hops = [dict(hop, line_start=i, line_end=i) for i in range(1, 22)]
    assert _errors(_set(full_paths_doc, ("paths", 1, "hops"), hops))
    assert _errors(_set(full_paths_doc, ("paths", 1, "hops"), hops[:20])) == []


@pytest.mark.parametrize("path", ["a/.hidden/b.py", "repo_b/cli.py", "pyproject.toml", "a..b/c.py"])
def test_schema_accepts_relative_paths_and_the_model_agrees(full_paths_doc, path):
    doc = _set(full_paths_doc, ("paths", 0, "entry", "location", "path"), path)
    assert _errors(doc) == []
    assert paths_name("PathsResult").model_validate(doc).paths[0].entry.location.path == path
