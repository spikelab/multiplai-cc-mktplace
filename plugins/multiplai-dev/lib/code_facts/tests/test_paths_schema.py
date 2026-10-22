"""schema/paths.v1.schema.json: header, strictness, and the documents it accepts and rejects."""

from __future__ import annotations

import copy
import importlib.resources
import json
import types
import typing
from pathlib import Path

import jsonschema
import pytest
from conftest import paths_name, unscoped_variant
from pydantic import BaseModel, ValidationError

FACTS_SCHEMA = (
    Path(__file__).resolve().parents[3] / "skills/review/schema/facts.v1.schema.json"
)
TOP_LEVEL = [
    "schema_version",
    "commit",
    "scoped",
    "cap",
    "fallback",
    "paths",
    "dropped",
    "diagnostics",
    "test_listing_skips",
]


def _schema() -> dict:
    ref = importlib.resources.files("code_facts").joinpath(
        "schema", "paths.v1.schema.json"
    )
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
    assert _props(schema, "paths") == [
        "id",
        "entry",
        "hops",
        "stops_at",
        "break_reason",
        "changed_lines_covered",
        "tests",
    ]
    assert _props(schema, "paths", "entry") == [
        "kind",
        "name",
        "location",
        "handler",
        "invocation",
        "invoked_by",
        "also_invoked_by",
    ]
    assert _props(schema, "paths", "entry", "location") == ["path", "line"]
    assert _props(schema, "paths", "entry", "handler") == ["path", "line", "symbol"]
    assert _props(schema, "paths", "entry", "also_invoked_by") == [
        "kind",
        "name",
        "location",
    ]
    assert _props(schema, "paths", "tests", "e2e") == [
        "node_id",
        "file",
        "line",
        "match",
        "invocation_line",
    ]
    assert _props(schema, "paths", "tests", "mocks_hop_2") == [
        "node_id",
        "file",
        "line",
        "match",
        "invocation_line",
        "patch_file",
        "patch_line",
    ]
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


def _literal_values(annotation) -> set:
    """The values of every Literal in `annotation`, looking through unions and list items."""
    if typing.get_origin(annotation) is typing.Literal:
        return set(typing.get_args(annotation))
    if typing.get_origin(annotation) in (typing.Union, types.UnionType, list):
        return set().union(*map(_literal_values, typing.get_args(annotation)))
    return set()


def _used_values(node, used: dict) -> None:
    """Record, per (model, field), every scalar value the parsed document holds there."""
    if isinstance(node, list):
        for item in node:
            _used_values(item, used)
    elif isinstance(node, BaseModel):
        for name in type(node).model_fields:
            value = getattr(node, name)
            if isinstance(value, (BaseModel, list)):
                _used_values(value, used)
            else:
                used.setdefault((type(node).__name__, name), set()).add(value)


def _enum_fields(model: type[BaseModel], seen: dict) -> None:
    """Map (model, field) to its Literal values, for `model` and every model it contains."""
    if model.__name__ in {m for m, _ in seen}:
        return
    for name, field in model.model_fields.items():
        seen[(model.__name__, name)] = _literal_values(field.annotation)
        for arg in typing.get_args(field.annotation) or (field.annotation,):
            for inner in typing.get_args(arg) or (arg,):
                if isinstance(inner, type) and issubclass(inner, BaseModel):
                    _enum_fields(inner, seen)


def test_full_coverage_fixtures_validate_and_use_every_enum_value(full_paths_doc):
    # A document holds one fallback status, so the other two come from variants.
    docs = [
        full_paths_doc,
        *(
            _set(full_paths_doc, ("fallback",), {"status": status, "reason": None})
            for status in ("ok", "skipped")
        ),
    ]
    result_cls = paths_name("PathsResult")
    used: dict = {}
    for doc in docs:
        assert _errors(doc) == []
        _used_values(result_cls.model_validate(doc), used)
    enums: dict = {}
    _enum_fields(result_cls, enums)
    missing = {
        where: sorted(values - used.get(where, set()))
        for where, values in enums.items()
    }
    assert {where: values for where, values in missing.items() if values} == {}


@pytest.mark.parametrize(
    "status,reason", [("ok", None), ("skipped", None), ("failed", "bad answer")]
)
def test_every_fallback_status_validates(full_paths_doc, status, reason):
    doc = _set(full_paths_doc, ("fallback",), {"status": status, "reason": reason})
    assert _errors(doc) == []


@pytest.mark.parametrize(
    "path,value",
    [
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
    ],
)
def test_schema_rejects(full_paths_doc, path, value):
    assert _errors(_set(full_paths_doc, path, value)), f"{path}={value!r} was accepted"


BAD_PATHS = ["/abs/x.py", "../x.py", "a/../b.py", "./x.py", "a\\b.py", ""]
# Every field holding a snapshot-relative path.
PATH_FIELDS = [
    ("paths", 0, "entry", "location", "path"),
    ("paths", 0, "entry", "handler", "path"),
    ("paths", 0, "hops", 0, "path"),
    ("paths", 0, "changed_lines_covered", 0, "path"),
    ("paths", 0, "tests", "e2e", 0, "file"),
    ("paths", 0, "tests", "mocks_hop_2", 0, "file"),
    ("paths", 0, "tests", "mocks_hop_2", 0, "patch_file"),
    ("diagnostics", 0, "path"),
    ("test_listing_skips", 0, "file"),
]


@pytest.mark.parametrize("field", PATH_FIELDS)
@pytest.mark.parametrize("value", BAD_PATHS)
def test_schema_and_model_reject_bad_paths(full_paths_doc, field, value):
    doc = _set(full_paths_doc, field, value)
    assert _errors(doc), f"schema accepted {field}={value!r}"
    with pytest.raises(ValidationError):
        paths_name("PathsResult").model_validate(doc)


def test_more_than_twenty_hops_is_rejected(full_paths_doc):
    hop = full_paths_doc["paths"][1]["hops"][0]
    hops = [dict(hop, line_start=i, line_end=i) for i in range(1, 22)]
    assert _errors(_set(full_paths_doc, ("paths", 1, "hops"), hops))
    assert _errors(_set(full_paths_doc, ("paths", 1, "hops"), hops[:20])) == []


@pytest.mark.parametrize(
    "path", ["a/.hidden/b.py", "repo_b/cli.py", "pyproject.toml", "a..b/c.py"]
)
def test_schema_accepts_relative_paths_and_the_model_agrees(full_paths_doc, path):
    doc = _set(full_paths_doc, ("paths", 0, "entry", "location", "path"), path)
    assert _errors(doc) == []
    assert (
        paths_name("PathsResult").model_validate(doc).paths[0].entry.location.path
        == path
    )
